from concurrent.futures import ProcessPoolExecutor, as_completed
import pandas as pd
import os
import sys
import numpy as np
import matplotlib.pyplot as plt
from tqdm import tqdm
import argparse
import random
from lmfit import Minimizer, Parameters
from lmfit.models import LinearModel

plt.style.use('seaborn-v0_8-whitegrid')
DEFAULT_MIN_LINEAR_POINTS = 1
DEFAULT_MIN_NOISE_POINTS = 2

plt.style.use('seaborn-whitegrid')

np.random.seed(8888)
random.seed(8888)

# Force warnings (other than FutureWarning) to kill the script; this allows debugging numpy warnings.
#import warnings
#warnings.simplefilter("error")
#warnings.simplefilter("ignore", FutureWarning)

# detect whether the file is Encyclopedia output or Skyline report, then read it in appropriately
def read_input(filename, col_conc_map_file):
    with open(filename, 'r') as f:
        header_line = f.readline()

    # if numFragments is a column, it's an Encyclopedia file
    if 'numFragments' in header_line:
        sys.stdout.write('Input identified as EncyclopeDIA *.elib.peptides.txt filetype.\n')

        df = pd.read_csv(filename, sep=None, engine='python')
        df.drop(['numFragments', 'Protein'], axis="columns", inplace=True)  # make a quantitative df with just curve points and peptides

        col_conc_map = pd.read_csv(col_conc_map_file, index_col="filename")

        # map filenames to concentrations
        df.rename(columns=dict(
            col_conc_map['concentration'],
            **{'Peptide': 'peptide'}
        ), inplace=True)

        df_melted = pd.melt(df, id_vars=['peptide'])
        df_melted.columns = ['peptide', 'curvepoint', 'area']
        df_melted = df_melted[df_melted['curvepoint'].isin(col_conc_map['concentration'])]

    # require columns for File Name, Total Area Fragment, Peptide Sequence
    # TODO: option for Total Area Ratio?
    elif all(col in header_line for col in ['Total Area Fragment', 'Peptide Sequence', 'File Name']):
        sys.stdout.write('Input identified as Skyline export filetype. \n')

        df_melted = pd.read_csv(filename)
        df_melted.rename(columns={'File Name': 'filename'}, inplace=True)
        col_conc_map = pd.read_csv(col_conc_map_file)

        # remove any data for which there isn't a map key
        df_melted = df_melted[df_melted['filename'].isin(col_conc_map['filename'])]

        # map filenames to concentrations
        df_melted = pd.merge(df_melted, col_conc_map, on='filename', how='outer')

        # clean up column names to match downstream convention
        df_melted.rename(columns={'Total Area Fragment': 'area',
                                  'Peptide Sequence': 'peptide',
                                  'concentration': 'curvepoint'}, inplace=True)

        # remove points that didn't have a mapping (NA)
        df_melted['curvepoint'].replace('', np.nan, inplace=True)
        df_melted.dropna(subset=['curvepoint'], inplace=True)

        df_melted['area'].fillna(0, inplace=True)  # replace NA with 0

    # dia-nn output
    elif 'Stripped.Sequence' in header_line:
        sys.stdout.write('Input identified as DIA-NN *.pr_matrix.tsv filetype.\n')

        df = pd.read_table(filename, sep=None, engine='python')
        df = df.drop(['Protein.Group',
            'Protein.Ids',
            'Protein.Names',
            'Genes',
            'First.Protein.Description',
            'Proteotypic',
            'Stripped.Sequence',
            'Precursor.Charge',
            'Precursor.Id'], 1)  # make a quantitative df with just curve points and peptides
        col_conc_map = pd.read_csv(col_conc_map_file)
        df = df.rename(columns=col_conc_map.set_index('filename')['concentration'])  # map filenames to concentrations
        df = df.rename(columns={'Modified.Sequence': 'peptide'})
        df_melted = pd.melt(df, id_vars=['peptide'])
        df_melted.columns = ['peptide', 'curvepoint', 'area']
        df_melted = df_melted[df_melted['curvepoint'].isin(col_conc_map['concentration'])]

        # remove colons in Unimod description, e.g. "AAVDC(UniMod:4)EC(UniMod:4)EFQNLEHNEK.png"
        df_melted['peptide'] = df_melted['peptide'].str.replace(':', '')
        #print(df_melted.head())

    # convert the curve points to numbers so that they sort correctly
    df_melted['curvepoint'] = pd.to_numeric(df_melted['curvepoint'])

    # replace NaN values with zero
    # TODO: is this appropriate? it's required for lmfit in any case
    df_melted['area'].fillna(0, inplace=True)

    return df_melted


# associates a multiplier value to the curvepoint a la single-point calibration
def associate_multiplier(df, multiplier_file):
    mutliplier_df = pd.read_csv(multiplier_file)

    # merge the multiplier with the data frame
    merged_df = pd.merge(df, mutliplier_df, on='peptide', how='inner')
    merged_df['curvepoint_multiplied'] = merged_df['curvepoint'] * merged_df['multiplier']
    multiplied_df = merged_df[['peptide', 'curvepoint_multiplied', 'area']]
    multiplied_df.columns = ['peptide', 'curvepoint', 'area']

    return multiplied_df


def linregress(data):
    x = data["curvepoint"]
    y = data["area"]
    w = data["weight"]

    model = LinearModel()

    pars = model.guess(y, x=x)
    result = model.fit(y, pars, x=x, weights=w)

    return result.params["slope"].value, result.params["intercept"].value


# yang's solve for the piecewise fit using lmfit Minimize function
def fit_by_lmfit_yang(x, y):

    def fcn2min(params, x, data, weight):
        a = params['a'].value
        b = params['b'].value
        c = params['c'].value
        model = np.maximum(c, a*x+b)
        return (model-data) * weight

    # parameter initialization
    def initialize_params(x, y, weights):
        subsetdf = pd.DataFrame({"curvepoint": pd.to_numeric(x), "area": y, "weight": weights})

        # Initial guess for where noise is
        curvepoints = list(sorted(subsetdf["curvepoint"].unique()))
        noise_mask = subsetdf["curvepoint"].apply(lambda x: x in curvepoints[:2])

        noise_intercept = np.mean(subsetdf["area"][noise_mask])

        # Use linear regression above intersection
        reg_data = subsetdf[~noise_mask]
        linear_slope, linear_intercept = linregress(reg_data)

        if noise_intercept <= linear_intercept:
            noise_intercept = linear_intercept * 1.05

        return linear_slope, linear_intercept, noise_intercept

    weights = np.minimum(1 / (np.asarray(np.sqrt(x), dtype=float)+np.finfo(float).eps), 1000)  # inverse weights

    params = Parameters()
    initial_a, initial_b, initial_c = initialize_params(x,y,weights)
    initial_cminusb = initial_c - initial_b
    params.add('a', value=initial_a, min=0.0, vary=True)  # slope signal
    params.add('b', value=initial_b, vary=True)  # intercept signal
    params.add('c_minus_b', value=initial_cminusb, min=0.0, vary=True)
    params.add('c', expr='b + c_minus_b')

    minner = Minimizer(fcn2min, params, fcn_args=(x, y, weights))
    result = minner.minimize()

    return result, minner


# find the intersection of the noise and linear regime
def calculate_lod(model_params, df, std_mult, min_noise_points, min_linear_points):

    m_noise, b_noise, m_linear, b_linear = model_params

    # calculate the standard deviation for the noise segment
    if (m_noise - m_linear) == 0:
        intersection = np.inf
    else:
        intersection = (b_linear-b_noise) / (m_noise-m_linear)

    std_noise = np.std(df['area'].loc[(df['curvepoint'].astype(float) < intersection)])

    min_curvepoint = df["curvepoint"].min()
    if intersection <= min_curvepoint and min_noise_points < 1:
        LOD = min_curvepoint
        std_noise = np.nan
    elif m_linear <= 0:  # catch edge cases where there is only noise in the curve
        LOD = float('Inf')
    else:
        LOD = (b_noise + (std_mult*std_noise) - b_linear) / m_linear
    lod_results = [LOD, std_noise]

    # LOD edge cases
    mask = df["curvepoint"] >= LOD
    if df["curvepoint"][mask].nunique() < min_linear_points:
        # if the intersection is higher than the top point of the curve
        lod_results = [np.inf, np.inf]
    elif df["curvepoint"][~mask].nunique() < min_noise_points:
        # if there's not enough below the LOD
        lod_results = [np.inf, np.inf]

    return lod_results


# find the intersection of the noise and linear regime
def calculate_loq(model_params, boot_results, cv_thresh=0.2):

    # initialize the known LOD and a 'blank' LOQ
    LOD = model_params[4]
    LOQ = float('Inf')

    if boot_results.empty:
        LOQ = float('Inf')
    else:
        # subset the bootstrap results for just those values above the LOD
        boot_subset = boot_results[(boot_results['boot_x'] > LOD)]

        # Mask picking out good CVs
        good_cv = boot_subset["boot_cv"] < cv_thresh

        if 0 == good_cv.sum():
            LOQ = float('Inf')
        else:
            # LOQ is the larger of:
            #   - smallest intensity with good CV
            #   - largest intensity with bad CV
            LOQ = max(
                boot_subset[good_cv]['boot_x'].min(),
                boot_subset[~good_cv]['boot_x'].max()
            )

            # LOQ edge cases
            if LOQ >= boot_results['boot_x'].max() or LOQ <= 0:
                LOQ = float('Inf')

    return LOQ


# determine prediction interval by bootstrapping
def bootstrap_many(df, new_x, num_bootreps=100):

    def bootstrap_once(df, new_x, iter_num):

#        resampled_df = df.sample(n=len(df), replace=True)

        while True:
            resampled_df = df.sample(n=len(df), replace=True)
            if resampled_df['area'].nunique() > 1:
                break

        boot_x = np.array(resampled_df['curvepoint'], dtype=float)
        boot_y = np.array(resampled_df['area'], dtype=float)
        fit_result, mini_result = fit_by_lmfit_yang(boot_x, boot_y)

        a = fit_result.params['a'].value
        b = fit_result.params['b'].value
        c = fit_result.params['c'].value

        yresults = np.maximum(new_x * a + b, c)

        iter_results = pd.DataFrame(data={'boot_x': new_x, iter_num: yresults})

        return iter_results

    if df.empty or np.isnan(new_x).any():
        boot_summary = pd.DataFrame(columns=['boot_x', 'count', 'mean', 'std', 'min',
                                             '5%', '50%', '95%', 'max', 'boot_cv'])

    else:
        # Bootstrap the data (e.g. resample the data with replacement), eval prediction (new_y) at each new_x
        boot_results = pd.DataFrame(data={'boot_x': new_x})
        for i in range(num_bootreps):
            iteration_results = bootstrap_once(df, new_x, i)

            boot_results = pd.merge(boot_results, iteration_results, on='boot_x')

        # reshape the bootstrap results to be columns=boot_x and rows=boot_y results (each iteration is a row)
        boot_results = boot_results.T
        boot_results.columns = boot_results.iloc[0]
        boot_results = boot_results.drop(['boot_x'], axis='rows')

        # calculate lower and upper 95% PI
        boot_summary = (boot_results.describe(percentiles=[.05, .95])).T
        boot_summary['boot_x'] = boot_summary.index

        # calculate the bootstrapped CV
        boot_summary['boot_cv'] = boot_summary['std']/boot_summary['mean']

    return boot_summary


# plot results
def build_plots(peptide, x, y, model_results, boot_results, num_bootreps, std_mult, output_dir):

    SMALL_SIZE = 18
    MEDIUM_SIZE = 20
    BIGGER_SIZE = 22

    plt.rc('font', size=SMALL_SIZE)  # controls default text sizes
    plt.rc('axes', titlesize=SMALL_SIZE)  # fontsize of the axes title
    plt.rc('axes', labelsize=MEDIUM_SIZE)  # fontsize of the x and y labels
    plt.rc('xtick', labelsize=SMALL_SIZE)  # fontsize of the tick labels
    plt.rc('ytick', labelsize=SMALL_SIZE)  # fontsize of the tick labels
    plt.rc('legend', fontsize=SMALL_SIZE)  # legend fontsize
    plt.rc('figure', titlesize=BIGGER_SIZE)  # fontsize of the figure title

    plt.figure(figsize=(5, 7))
    plt.suptitle(peptide)

    slope_noise, intercept_noise, slope_linear, intercept_linear, LOD, std_noise, LOQ = model_results

    # plot a line given a slope and intercept
    def add_line_to_plot(slope, intercept, scale, setstyle='-', setcolor='k'):
        axes = plt.gca()
        xlims = np.array(axes.get_xlim())
        x_vals = np.arange(xlims[0], xlims[1], ((xlims[1] - xlims[0]) / 100))
        y_vals = intercept + slope * x_vals
        if scale == 'semilogx':
            plt.semilogx(x_vals, y_vals, linestyle=setstyle, color=setcolor)
        elif scale == 'loglog':
            plt.loglog(x_vals, y_vals, linestyle=setstyle, color=setcolor)
        else:
            plt.plot(x_vals, y_vals, linestyle=setstyle, color=setcolor)

    ###
    ### top plot: linear scale x axis
    plt.subplot(2, 1, 1)
    plt.plot(x, y, 'o')  # scatterplot of the data
    if not boot_results.empty:
        plt.fill_between(boot_results['boot_x'],
                         boot_results['mean']-boot_results['std'], boot_results['mean']+boot_results['std'],
                         color='y', alpha=0.3)
    add_line_to_plot(slope_noise, intercept_noise, 'linear', '-', 'g')  # add noise segment line
    add_line_to_plot(slope_noise, (intercept_noise + (std_mult*std_noise)), 'linear', '--', setcolor='0.5')
    if slope_linear > 0:  # add linear segment line
        add_line_to_plot(slope_linear, intercept_linear, 'linear', '-', 'g')

    plt.axvline(x=LOD,
                color='m',
                label=('LOD = %.3e' % LOD))

    plt.axvline(x=LOQ,
                color='c',
                label=('LOQ = %.3e' % LOQ))

    plt.ylabel('signal')

    # force axis ticks to be scientific notation so the plot is prettier
    plt.ticklabel_format(style='sci', axis='both', scilimits=(0, 0))

    plt.xlim(xmin=min(x)-max(x)*0.01)  # anchor x and y to 0-ish
    plt.ylim(ymin=min(y)-max(y)*0.01, ymax=(max(y))*1.05)


    ###
    ### bottom plot: bootstrapped CVs for discretized points
    plt.subplot(2, 1, 2)
    plt.plot(boot_results['boot_x'], boot_results['boot_cv'], marker=None, color='k', label='_nolegend_')

    # plot actual CVs at each curve point
    df = pd.DataFrame({"curvepoint": x, "area": y})

    # Instead of direct CV, match prediction boostrap and take CV of resampled _means_
    means = pd.DataFrame()
    for i in range(num_bootreps):
        # resample
        resampled_df = df.sample(n=len(df), replace=True)

        # get the mean for each point
        resampled_means = resampled_df.groupby("curvepoint").mean()

        # append to frame
        means = pd.concat([means, resampled_means], axis="rows")

    # Compute the average/std across boostrapped means for each point
    groups = means.groupby("curvepoint").agg({'area': ["mean", "std"]})
    cv = groups[("area", "std")] / groups[("area", "mean")]

    plt.scatter(cv.index, cv, marker="o", color="tab:blue", label="_nolegend_")

    plt.axvline(x=LOD,
                color='m',
                label=('LOD = %.3e' % LOD))

    plt.axvline(x=LOQ,
                color='c',
                label=('LOQ = %.3e' % LOQ))

    # add 20%CV reference line
    plt.axhline(y=0.20, color='r', linestyle='dashed')

    #plt.title(peptide, y=1.08)
    plt.xlabel('quantity')
    plt.ylabel('CV')

    # force axis ticks to be scientific notation so the plot is prettier
    plt.ticklabel_format(style='sci', axis='x', scilimits=(0, 0))

    plt.xlim(xmin=min(x)-max(x)*0.01)  # anchor x and y to 0-ish.
    if len(boot_results['boot_cv']) > 0:
        plt.ylim(ymin=-0.01,
                 ymax=(max(boot_results['boot_cv'].max(), cv.max())*1.05))

    # save the figure
    # add legend with LOD and LOQ values
    legend = plt.legend(loc=8, bbox_to_anchor=(0, -.75, 1., .102), ncol=2)
    plt.savefig(os.path.join(output_dir, peptide + '.png'),
                bbox_extra_artists=(legend,),
                bbox_inches='tight', pad_inches=0.75)
    #plt.show()
    plt.close()


def process_peptide(bootreps, cv_thresh, output_dir, peptide, plot_or_not, std_mult, min_noise_points, min_linear_points, subset, verbose):
    # sort the dataframe with x values in strictly ascending order
    subset = subset.sort_values(by='curvepoint', ascending=True)

    # create the x and y arrays
    x = np.array(subset['curvepoint'], dtype=float)
    y = np.array(subset['area'], dtype=float)

    # set up the model and the parameters (yang's lmfit minimize function approach)
    result, mini = fit_by_lmfit_yang(x, y)
    slope_noise = 0.0
    slope_linear = result.params['a'].value
    intercept_linear = result.params['b'].value
    intercept_noise = result.params['c'].value

    model_parameters = np.asarray([slope_noise, intercept_noise, slope_linear, intercept_linear])

    lod_vals = calculate_lod(model_parameters, subset, std_mult, min_noise_points, min_linear_points)
    LOD, std_noise = lod_vals

    model_parameters = np.append(model_parameters, lod_vals)

    if not np.isfinite(LOD):
        LOQ = np.inf
        bootstrap_df = bootstrap_many(subset, [np.nan], num_bootreps=0)  # shortcut to get empty DF
    else:
        # calculate coefficients of variation for discrete bins over the linear range (default bins=100)
        x_i = np.linspace(LOD, max(x), num=100, dtype=float)

        bootstrap_df = bootstrap_many(subset, new_x=x_i, num_bootreps=bootreps)

        if verbose == 'y':
            # TODO: this line appears to be very buggy
            boot_summary.to_csv(path_or_buf=os.path.join(output_dir,
                                                         'bootstrapsummary_' + str(list(set(df['peptide']))[0]) + '.csv'),
                                index=True)

        LOQ = calculate_loq(model_parameters, bootstrap_df, cv_thresh)

    model_parameters = np.append(model_parameters, LOQ)

    if plot_or_not == 'y':
        # make a plot of the curve points and the fit, in both linear and log space
        # build_plots(x, y, model_parameters, bootstrap_df, std_mult)
        try:
            build_plots(peptide, x, y, model_parameters, bootstrap_df, bootreps, std_mult, output_dir)
            # continue
        except ValueError:
            sys.stderr.write('ERROR! Issue with peptide %s. \n' % peptide)

    # make a dataframe row with the peptide and its figures of merit
    new_row = [peptide, LOD, LOQ, slope_linear, intercept_linear, intercept_noise, std_noise]
    new_df_row = pd.DataFrame([new_row], columns=['peptide', 'LOD', 'LOQ',
                                                  'slope_linear', 'intercept_linear', 'intercept_noise',
                                                  'stndev_noise'])

    return new_df_row


def main():
    # usage statement and input descriptions
    parser = argparse.ArgumentParser(
        description='A  model for fitting calibration curve data. Takes calibration curve measurements as input, and \
                    returns the Limit of Detection (LOD) and Limit of Quantitation (LOQ) for each peptide measured in \
                    the calibration curve.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument('curve_data', type=str,
                        help='a matrix containing peptides and their quantitative values across each curve point (currently\
                                supporting Encyclopedia *.elib.peptides.txt quant reports and Skyline export reports)')
    parser.add_argument('filename_concentration_map', type=str,
                        help='a comma-delimited file containing maps between filenames and the concentration point \
                                they represent (two columns named "filename" and "concentration")')
    parser.add_argument('--std_mult', default=2, type=float,
                        help='specify a multiplier of the standard deviation of the noise for determining limit of \
                        detection (LOD)')
    parser.add_argument('--cv_thresh', default=0.2, type=float,
                        help='specify a coefficient of variation threshold for determining limit of quantitation (LOQ) \
                                (Note: this should be a decimal, not a percentage, e.g. 20%CV threshold should be input as \
                                0.2)')
    parser.add_argument('--bootreps', default=100, type=int,
                        help='specify a number of times to bootstrap the data (Note: this must be an integer, e.g. to \
                                resample the data 100 times, the parameter value should be input as 100')
    parser.add_argument('--min_noise_points', default=DEFAULT_MIN_NOISE_POINTS, type=int,
                        help="specify the minimum required curve points required below the LOD")
    parser.add_argument('--min_linear_points', default=DEFAULT_MIN_LINEAR_POINTS, type=int,
                        help="specify the minimum required curve points required above the LOD")
    parser.add_argument('--multiplier_file', type=str,
                        help='use a single-point multiplier associated with the curve data peptides')
    parser.add_argument('--output_path', default=os.getcwd(), type=str,
                        help='specify an output path for figures of merit and plots')
    parser.add_argument('--plot', default='y', type=str,
                        help='yes/no (y/n) to create individual calibration curve plots for each peptide')
    parser.add_argument('--verbose', default='n', type=str,
                        help='output a detailed summary of the bootstrapping step')

    # parse arguments from command line
    args = parser.parse_args()
    raw_file = args.curve_data
    col_conc_map_file = args.filename_concentration_map
    cv_thresh = args.cv_thresh
    std_mult = args.std_mult
    bootreps = args.bootreps
    min_noise_points = args.min_noise_points
    min_linear_points = args.min_linear_points
    multiplier_file = args.multiplier_file
    output_dir = args.output_path
    plot_or_not = args.plot
    verbose = args.verbose

    # read in the data
    quant_df_melted = read_input(raw_file, col_conc_map_file)

    # associate multiplier with the curvepoint ratio (if there is a multiplier provided)
    if multiplier_file:
        quant_df_melted = associate_multiplier(quant_df_melted, multiplier_file)

    # initialize empty data frame to store figures of merit
    peptide_fom = pd.DataFrame(columns=['peptide', 'LOD', 'LOQ',
                                        'slope_linear', 'intercept_linear', 'intercept_noise',
                                        'stndev_noise'])

    # and awwaayyyyy we go~
    with ProcessPoolExecutor() as exec:
        # First, submit each peptide as a job to the executor
        futures = []
        for peptide, subset in quant_df_melted.groupby('peptide'):
            if subset.empty:  # if the peptide is nan, skip it and move on to the next peptide
                continue

            futures.append(exec.submit(process_peptide, bootreps, cv_thresh, output_dir, peptide, plot_or_not, std_mult, min_noise_points, min_linear_points, subset, verbose))

        # Just loop over the resulting futures and build up the results
        for future in tqdm(as_completed(futures), total=len(futures)):
            peptide_fom = pd.concat([peptide_fom, future.result()], ignore_index=True, axis=0)

    peptide_fom.to_csv(path_or_buf=os.path.join(output_dir, 'figuresofmerit.csv'),
                       index=False)if __name__ == "__main__":
    main()