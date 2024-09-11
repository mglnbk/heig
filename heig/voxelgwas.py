import os
import numpy as np
import threading
import concurrent.futures
from scipy.stats import chi2
from tqdm import tqdm
from heig import sumstats
import heig.input.dataset as ds



class VGWAS:
    def __init__(self, bases, ldr_cov, ldr_gwas, snp_idxs, n, threads):
        """
        Parameters:
        ------------
        bases: a np.array of bases (N, r)
        ldr_cov: a np.array of variance-covariance matrix of LDRs (r, r)
        ldr_gwas: a GWAS instance
        snp_idxs: numerical indices of SNPs to extract (d, )
        n: sample sizes of SNPs (d, 1)
        threads: number of threads
        
        """
        self.bases = bases
        self.ldr_cov = ldr_cov
        self.ldr_gwas = ldr_gwas
        self.ldr_idxs = list(range(ldr_gwas.n_gwas))
        self.snp_idxs = snp_idxs
        self.n = n
        self.ztz_inv = self._compute_ztz_inv(threads) # (d, 1)

    def _compute_ztz_inv(self, threads):
        """
        Computing (Z'Z)^{-1} from summary statistics

        Parameters:
        ------------
        threads: number of threads

        Returns:
        ---------
        ztz_inv: a np.array of (Z'Z)^{-1} (d, 1)
        
        """
        ldr_var = np.diag(self.ldr_cov)
        ztz_inv = np.zeros(np.sum(self.snp_idxs), dtype=np.float32)
        n_ldrs = self.bases.shape[1]

        futures = []
        i = 0
        data_reader = self.ldr_gwas.data_reader('both', self.ldr_idxs, self.snp_idxs, all_gwas=False)
        lock = threading.Lock()
    
        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
            for ldr_beta_batch, ldr_z_batch in data_reader:
                futures.append(executor.submit(self._compute_ztz_inv_batch, ztz_inv, ldr_beta_batch, ldr_z_batch, ldr_var, i, lock))
                batch_size = ldr_beta_batch.shape[1]
                i += batch_size

            for future in concurrent.futures.as_completed(futures):
                future.result()

        ztz_inv /= n_ldrs
        ztz_inv = ztz_inv.reshape(-1, 1)
        
        return ztz_inv
    
    def _compute_ztz_inv_batch(self, ztz_inv, ldr_beta_batch, ldr_z_batch, ldr_var, i, lock):
        """
        Computing (Z'Z)^{-1} from summary statistics in batch
        
        """
        ldr_se_batch = ldr_beta_batch / ldr_z_batch
        batch_size = ldr_beta_batch.shape[1]
        ztz_inv_batch = np.sum(
            (ldr_se_batch * ldr_se_batch + ldr_beta_batch * ldr_beta_batch / self.n) / ldr_var[i: i+batch_size], 
            axis=1
        )
        with lock:
            ztz_inv += ztz_inv_batch
    
    def recover_beta(self, voxel_idxs, threads):
        """
        Recovering voxel beta

        Parameters:
        ------------
        voxel_idxs: a list of voxel idxs (q)

        Returns:
        ---------
        voxel_beta: a np.array of voxel beta (d, q)
        
        """
        voxel_beta = np.zeros((np.sum(self.snp_idxs), len(voxel_idxs)), dtype=np.float32)
        data_reader = self.ldr_gwas.data_reader('beta', self.ldr_idxs, self.snp_idxs, all_gwas=False)
        base = self.bases[voxel_idxs] # (q, r)

        lock = threading.Lock()
        i = 0
        futures = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
            for ldr_beta_batch in data_reader:
                futures.append(executor.submit(self._recover_beta_batch, voxel_beta, ldr_beta_batch, base, i, lock))
                batch_size = ldr_beta_batch.shape[1]
                i += batch_size
            
            for future in concurrent.futures.as_completed(futures):
                future.result()

        return voxel_beta
    
    def _recover_beta_batch(self, voxel_beta, ldr_beta_batch, base, i, lock):
        """
        Computing voxel beta in batch
        
        """
        batch_size = ldr_beta_batch.shape[1]
        voxel_beta_batch = np.dot(ldr_beta_batch, base[:, i: i+batch_size].T)
        with lock:
            voxel_beta += voxel_beta_batch
    
    def recover_se(self, voxel_idxs, voxel_beta):
        """
        Recovering standard errors for voxel-level genetic effects

        Parameters:
        ------------
        voxel_idx: a list of voxel idxs (q)
        voxel_beta: a np.array of voxel beta (d, q)

        Returns:
        ---------
        voxel_se: a np.array of standard errors for voxel-level genetic effects (d, )

        """
        base = np.atleast_2d(self.bases[voxel_idxs]) # (q, r)
        part1 = np.sum(np.dot(base, self.ldr_cov) * base, axis=1) # (q, )
        voxel_beta_squared = voxel_beta * voxel_beta
        voxel_beta_squared /= self.n
        voxel_se = part1 * self.ztz_inv
        voxel_se -= voxel_beta_squared
        np.sqrt(voxel_se, out=voxel_se)
        # voxel_se = np.sqrt(part1 * self.ztz_inv - voxel_beta * voxel_beta / self.n) # slow

        return voxel_se


def voxel_reader(n_snps, voxel_list):
    """
    Doing voxel GWAS in batch, each block less than 3 GB
    
    """
    n_voxels = len(voxel_list)
    memory_use = n_snps * n_voxels * np.dtype(np.float32).itemsize / (1024 ** 3)
    if memory_use <= 3:
        batch_size = n_voxels
    else:
        batch_size = int(n_voxels / memory_use * 3)

    for i in range(0, n_voxels, batch_size):
        yield voxel_list[i: i+batch_size]


def write_header(snp_info, outpath):
    """
    Writing output header

    """
    output_header = snp_info.head(0).copy()
    output_header.insert(0, 'INDEX', None)
    output_header['BETA'] = None
    output_header['SE'] = None
    output_header['Z'] = None
    output_header['P'] = None
    output_header = output_header.to_csv(sep='\t', header=True, index=None)
    with open(outpath, 'w') as file:
        file.write(output_header)


def _process_voxels_batch(i, voxel_idx, all_sig_idxs, snp_info, 
                  voxel_beta, voxel_se, voxel_z, all_sig_idxs_voxel
    ):
    """
    Processing each voxel
    
    """
    if all_sig_idxs_voxel[i]:
        sig_idxs = all_sig_idxs[:, i]
        sig_snps = snp_info.loc[sig_idxs].copy()
        sig_snps['BETA'] = voxel_beta[sig_idxs, i]
        sig_snps['SE'] = voxel_se[sig_idxs, i]
        sig_snps['Z'] = voxel_z[sig_idxs, i]
        sig_snps['P'] = chi2.sf(sig_snps['Z'] ** 2, 1)
        sig_snps.insert(0, 'INDEX', [voxel_idx+1] * np.sum(sig_idxs))
        sig_snps_output = sig_snps.to_csv(sep='\t', header=False, na_rep='NA', index=None, float_format='%.5e')
        return i, sig_snps_output
    return i, None


def process_voxels(
        voxel_idxs, all_sig_idxs, snp_info, voxel_beta, 
        voxel_se, voxel_z, all_sig_idxs_voxel, outpath, threads
    ):
    """
    Processing voxels in parallel

    Parameters:
    ------------
    voxel_idxs: a list of voxel idxs (q)
    all_sig_idxs: a np.array of boolean significant indices (d, q)
    snp_info: a pd.DataFrame of all SNPs (d, x)
    voxel_beta: a np.array of voxel beta (d, q)
    voxel_se: a np.array of voxel se (d, q)
    voxel_z: a np.array of voxel z-score (d, q)
    all_sig_idxs_voxel: a np.array of boolean indices of any significant SNPs (q, )
    outpath: a directory of output
    threads: number of threads
    
    """
    results_dict = {}
    future_to_idx = {}
    next_write_i = 0
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
        for i, voxel_idx in enumerate(voxel_idxs):
            result = executor.submit(
                _process_voxels_batch, i, voxel_idx, all_sig_idxs, snp_info, 
                voxel_beta, voxel_se, voxel_z, all_sig_idxs_voxel
            )
            future_to_idx[result] = i
        
        for future in concurrent.futures.as_completed(future_to_idx):
            i, sig_snps_output = future.result()
            results_dict[i] = sig_snps_output
            while next_write_i in results_dict:
                if results_dict[next_write_i] is not None:
                    with open(outpath, 'a') as file:
                        file.write(results_dict.pop(next_write_i))
                next_write_i += 1


def check_input(args, log):
    # required arguments
    if args.ldr_sumstats is None:
        raise ValueError('--ldr-sumstats is required')
    if args.bases is None:
        raise ValueError('--bases is required')
    if args.ldr_cov is None:
        raise ValueError('--ldr-cov is required')

    # optional arguments
    if args.n_ldrs is not None and args.n_ldrs <= 0:
        raise ValueError('--n-ldrs should be greater than 0')
    if args.sig_thresh is not None and (args.sig_thresh <= 0 or args.sig_thresh >= 1):
        raise ValueError('--sig-thresh should be greater than 0 and less than 1')
    if args.range is None and args.voxel is None and args.sig_thresh is None and args.extract is None:
        log.info(('WARNING: generating all voxelwise summary statistics will require large disk space. '
                  'Specify a p-value threshold by --sig-thresh to screen out insignificant results.'))

    # required files must exist
    if not os.path.exists(f"{args.ldr_sumstats}.snpinfo"):
        raise FileNotFoundError(f"{args.ldr_sumstats}.snpinfo does not exist")
    if not os.path.exists(f"{args.ldr_sumstats}.sumstats"):
        raise FileNotFoundError(f"{args.ldr_sumstats}.sumstats does not exist")
    if not os.path.exists(args.bases):
        raise FileNotFoundError(f"{args.bases} does not exist")
    if not os.path.exists(args.ldr_cov):
        raise FileNotFoundError(f"{args.ldr_cov} does not exist")

    # process some arguments
    if args.range is not None:
        try:
            start, end = args.range.split(',')
            start_chr, start_pos = [int(x) for x in start.split(':')]
            end_chr, end_pos = [int(x) for x in end.split(':')]
        except:
            raise ValueError('--range should be in this format: <CHR>:<POS1>,<CHR>:<POS2>')
        if start_chr != end_chr:
            raise ValueError((f'starting with chromosome {start_chr} '
                              f'while ending with chromosome {end_chr} '
                              'is not allowed'))
        if start_pos > end_pos:
            raise ValueError((f'starting with {start_pos} '
                              f'while ending with position is {end_pos} '
                              'is not allowed'))
    else:
        start_chr, start_pos, end_chr, end_pos = None, None, None, None

    if args.extract is not None:
        keep_snps = ds.read_extract(args.extract)
    else:
        keep_snps = None

    return start_chr, start_pos, end_pos, keep_snps


def run(args, log):
    # checking input
    target_chr, start_pos, end_pos, keep_snps = check_input(args, log)

    # reading data
    ldr_cov = np.load(args.ldr_cov)
    log.info(f'Read variance-covariance matrix of LDRs from {args.ldr_cov}')
    bases = np.load(args.bases)
    log.info(f'{bases.shape[1]} bases read from {args.bases}')

    try:
        ldr_gwas = sumstats.read_sumstats(args.ldr_sumstats)
        log.info(f'{ldr_gwas.n_snps} SNPs read from LDR summary statistics {args.ldr_sumstats}')

        # keep selected LDRs
        if args.n_ldrs is not None:
            bases, ldr_cov, ldr_gwas = ds.keep_ldrs(args.n_ldrs, bases, ldr_cov, ldr_gwas)
            log.info(f'Keep the top {args.n_ldrs} LDRs.')

        if bases.shape[1] != ldr_cov.shape[0] or bases.shape[1] != ldr_gwas.n_gwas:
            raise ValueError(('inconsistent dimension for bases, variance-covariance matrix of LDRs, '
                              'and LDR summary statistics. '
                              'Try to use --n-ldrs'))

        # getting the outpath and SNP list
        outpath = args.out
        if args.voxel is not None:
            if np.max(args.voxel) + 1 <= bases.shape[0] and np.min(args.voxel) >= 0:
                log.info(f'{len(args.voxel)} voxels included.')
            else:
                raise ValueError('--voxel index (one-based) out of range')
        else:
            args.voxel = np.arange(bases.shape[0])

        if target_chr:
            snp_idxs = ((ldr_gwas.snpinfo['POS'] > start_pos) & (ldr_gwas.snpinfo['POS'] < end_pos) &
                (ldr_gwas.snpinfo['CHR'] == target_chr)).to_numpy()
            outpath += f"_chr{target_chr}_start{start_pos}_end{end_pos}.txt"
            log.info(f'{np.sum(snp_idxs)} SNP(s) on chromosome {target_chr} from {start_pos} to {end_pos}.')
        else:
            snp_idxs = ~ldr_gwas.snpinfo['SNP'].isna().to_numpy()
            outpath += ".txt"
            log.info(f'{np.sum(snp_idxs)} SNP(s) in total.')
        
        if keep_snps is not None:
            idx_keep_snps = (ldr_gwas.snpinfo['SNP'].isin(keep_snps['SNP'])).to_numpy()
            snp_idxs = snp_idxs & idx_keep_snps
            log.info(f"Keep {len(keep_snps['SNP'])} SNP(s) from --extract.")

        # extracting SNPs
        ldr_n = np.array(ldr_gwas.snpinfo['N']).reshape(-1, 1)
        ldr_n = ldr_n[snp_idxs]
        snp_info = ldr_gwas.snpinfo.loc[snp_idxs]

        # getting threshold
        if args.sig_thresh:
            thresh_chisq = chi2.ppf(1 - args.sig_thresh, 1)
        else:
            thresh_chisq = 0

        # doing analysis
        log.info(f"Recovering voxel-level GWAS results ...")
        write_header(snp_info, outpath)
        vgwas = VGWAS(bases, ldr_cov, ldr_gwas, snp_idxs, ldr_n, args.threads)
        
        for voxel_idxs in tqdm(voxel_reader(np.sum(snp_idxs), args.voxel), desc=f"Doing GWAS for {len(args.voxel)} voxels in batch"):
            voxel_beta = vgwas.recover_beta(voxel_idxs, args.threads)
            voxel_se = vgwas.recover_se(voxel_idxs, voxel_beta)
            voxel_z = voxel_beta / voxel_se
            all_sig_idxs = voxel_z * voxel_z >= thresh_chisq
            all_sig_idxs_voxel = all_sig_idxs.any(axis=0)

            process_voxels(
                voxel_idxs, all_sig_idxs, snp_info, voxel_beta, 
                voxel_se, voxel_z, all_sig_idxs_voxel, outpath, args.threads
            )

        log.info(f"\nSave the output to {outpath}")

    finally:
        ldr_gwas.close()
