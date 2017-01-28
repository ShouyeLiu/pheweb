#!/usr/bin/env python2

from __future__ import print_function, division, absolute_import

from .. import utils
conf = utils.conf

import glob
import os
import json
import math
import datetime
import multiprocessing
import scipy.stats
import collections
import csv


NEGLOG10_PVAL_BIN_SIZE = 0.05 # Use 0.05, 0.1, 0.15, etc
NEGLOG10_PVAL_BIN_DIGITS = 2 # Then round to this many digits
NUM_BINS = 1000

NUM_MAF_RANGES = 4

Variant = collections.namedtuple('Variant', ['neglog10_pval', 'maf'])
def get_variants(f):
    for v in csv.DictReader(f, delimiter='\t'):
        pval = v['pval']
        try:
            pval = float(pval)
        except ValueError:
            continue
        maf = float(v['maf'])
        yield Variant(-math.log10(pval), maf)


def approx_equal(a, b, tolerance=1e-4):
    return abs(a-b) <= max(abs(a), abs(b)) * tolerance
assert approx_equal(42, 42.0000001)
assert not approx_equal(42, 42.01)

def gc_value_from_list(neglog10_pvals, quantile=0.5):
    # neglog10_pvals must be in decreasing order.
    assert all(neglog10_pvals[i] >= neglog10_pvals[i+1] for i in range(len(neglog10_pvals)-1))
    neglog10_pval = neglog10_pvals[int(len(neglog10_pvals) * quantile)]
    pval = 10 ** -neglog10_pval
    return gc_value(pval, quantile)
def gc_value(pval, quantile=0.5):
    # This should be equivalent to this R: `qchisq(p, df=1, lower.tail=F) / qchisq(.5, df=1, lower.tail=F)`
    return scipy.stats.chi2.ppf(1 - pval, 1) / scipy.stats.chi2.ppf(1 - quantile, 1)
assert approx_equal(gc_value(0.49), 1.047457) # I computed these using that R code.
assert approx_equal(gc_value(0.5), 1)
assert approx_equal(gc_value(0.50001), 0.9999533)
assert approx_equal(gc_value(0.6123), 0.5645607)


def compute_qq(neglog10_pvals):
    # neglog10_pvals must be in decreasing order.
    assert all(neglog10_pvals[i] >= neglog10_pvals[i+1] for i in range(len(neglog10_pvals)-1))

    if len(neglog10_pvals) == 0:
        return []

    max_exp_neglog10_pval = -math.log10(0.5 / len(neglog10_pvals))
    max_obs_neglog10_pval = neglog10_pvals[0]

    if max_obs_neglog10_pval == 0:
        print('WARNING: All pvalues are 1! How is that supposed to make a QQ plot?')
        return []

    occupied_bins = set()
    for i, obs_neglog10_pval in enumerate(neglog10_pvals):
        exp_neglog10_pval = -math.log10( (i+0.5) / len(neglog10_pvals))
        exp_bin = int(exp_neglog10_pval / max_exp_neglog10_pval * NUM_BINS)
        obs_bin = int(obs_neglog10_pval / max_obs_neglog10_pval * NUM_BINS)
        occupied_bins.add( (exp_bin,obs_bin) )

    qq = []
    for exp_bin, obs_bin in occupied_bins:
        assert 0 <= exp_bin <= NUM_BINS, exp_bin
        assert 0 <= obs_bin <= NUM_BINS, obs_bin
        qq.append((
            exp_bin / NUM_BINS * max_exp_neglog10_pval,
            obs_bin / NUM_BINS * max_obs_neglog10_pval
        ))
    return sorted(qq)


def make_qq_stratified(variants):
    variants = sorted(variants, key=lambda v: v.maf)

    qqs = [dict() for i in range(NUM_MAF_RANGES)]
    for qq_i in range(NUM_MAF_RANGES):
        # Note: slice_indices[1] is the same as slice_indices[0] of the next slice.
        # But that's not a problem, because range() ignores the last index.
        slice_indices = (len(variants) * qq_i//NUM_MAF_RANGES,
                         len(variants) * (qq_i+1)//NUM_MAF_RANGES)
        qqs[qq_i]['maf_range'] = (variants[slice_indices[0]].maf,
                                  variants[slice_indices[1]-1].maf)
        neglog10_pvals = sorted((variants[i].neglog10_pval for i in range(*slice_indices)), reverse=True)
        qqs[qq_i]['count'] = len(neglog10_pvals)
        qqs[qq_i]['qq'] = compute_qq(neglog10_pvals)

    return qqs


def make_qq(neglog10_pvals):
    neglog10_pvals = sorted(neglog10_pvals, reverse=True)
    rv = {}
    rv['qq'] = compute_qq(neglog10_pvals) # We don't need this now.
    rv['count'] = len(neglog10_pvals)
    rv['gc_lambda'] = {}
    rv['gc_lambda']['0.5'] = utils.round_sig(gc_value_from_list(neglog10_pvals, 0.5), 5)
    rv['gc_lambda']['0.1'] = utils.round_sig(gc_value_from_list(neglog10_pvals, 0.1), 5)
    rv['gc_lambda']['0.01'] = utils.round_sig(gc_value_from_list(neglog10_pvals, 0.01), 5)
    rv['gc_lambda']['0.001'] = utils.round_sig(gc_value_from_list(neglog10_pvals, 0.001), 5)
    return rv


@utils.exception_printer
def make_json_file(args):
    src_filename, dest_filename, tmp_filename = args['src'], args['dest'], args['tmp']
    try:

        with open(src_filename) as f:
            variants = list(get_variants(f))

        rv = {}
        if variants:
            rv['overall'] = make_qq(v.neglog10_pval for v in variants)
            rv['by_maf'] = make_qq_stratified(variants)

        # Avoid getting killed while writing dest_filename, to stay idempotent despite me frequently killing the program
        with open(tmp_filename, 'w') as f:
            json.dump(rv, f, sort_keys=True, indent=0)
            os.fsync(f.fileno()) # Recommended by <http://stackoverflow.com/a/2333979/1166306>
        print('{}\t{} -> {}'.format(datetime.datetime.now(), src_filename, dest_filename))
        os.rename(tmp_filename, dest_filename)
    except Exception as exc:
        print('ERROR OCCURRED WHEN MAKING QQ FILE {!r} FROM FILE {!r} (TMP FILE AT {!r})'.format(
            dest_filename, src_filename, tmp_filename))
        print('ERROR WAS:')
        print(exc)
        print('---')
        raise


def get_conversions_to_do():
    for src_filename in glob.glob(conf.data_dir + '/augmented_pheno/*'):
        phenocode = os.path.basename(src_filename)
        dest_filename = '{}/qq/{}.json'.format(conf.data_dir, phenocode)
        tmp_filename = '{}/tmp/qq-{}.json'.format(conf.data_dir, phenocode)
        if not os.path.exists(dest_filename) or os.stat(dest_filename).st_mtime < os.stat(src_filename).st_mtime:
            yield {'src':src_filename, 'dest':dest_filename, 'tmp':tmp_filename}

def run(argv):

    utils.mkdir_p(conf.data_dir + '/qq')
    utils.mkdir_p(conf.data_dir + '/tmp')

    conversions_to_do = list(get_conversions_to_do())
    print('number of phenos to process:', len(conversions_to_do))
    num_processes = multiprocessing.cpu_count() * 3//4 + 1
    p = multiprocessing.Pool(num_processes)
    p.map_async(make_json_file, conversions_to_do).get(1e8) # Makes KeyboardInterrupt work


if __name__ == '__main__':
    run([])