import os
from collections import OrderedDict
from datetime import timedelta, datetime
import pandas as pd
import numpy as np
import torch
from torch.utils.data.dataset import Dataset
from attrdict import AttrDict
from neuralprophet import hdays as hdays_part2
import holidays as hdays_part1
from collections import defaultdict
from neuralprophet import utils, df_utils


class TimeDataset(Dataset):
    """Create a PyTorch dataset of a tabularized time-series"""

    def __init__(self, *args, **kwargs):
        """Initialize Timedataset from time-series df.

        Args:
            *args (): identical to tabularize_univariate_datetime
            **kwargs (): identical to tabularize_univariate_datetime
        """
        self.length = None
        self.inputs = None
        self.targets = None
        self.two_level_inputs = ["seasonalities", "covariates"]
        inputs, targets = tabularize_univariate_datetime(*args, **kwargs)
        self.init_after_tabularized(inputs, targets)

    def init_after_tabularized(self, inputs, targets=None):
        """Create Timedataset with data.

        Args:
            inputs (ordered dict): identical to returns from tabularize_univariate_datetime
            targets (np.array, float): identical to returns from tabularize_univariate_datetime
        """
        inputs_dtype = {
            "time": torch.float,
            # "changepoints": torch.bool,
            "seasonalities": torch.float,
            'events': torch.float,
            "lags": torch.float,
            "covariates": torch.float,
        }
        targets_dtype = torch.float
        self.length = inputs["time"].shape[0]

        self.inputs = OrderedDict({})
        for key, data in inputs.items():
            if key in self.two_level_inputs or key == "events":
                self.inputs[key] = OrderedDict({})
                for name, features in data.items():
                    self.inputs[key][name] = torch.from_numpy(features).type(inputs_dtype[key])
            else:
                self.inputs[key] = torch.from_numpy(data).type(inputs_dtype[key])
        self.targets = torch.from_numpy(targets).type(targets_dtype)

    def __getitem__(self, index):
        """Overrides parent class method to get an item at index.

        Args:
            index (int): sample location in dataset

        Returns:
            sample (OrderedDict): model inputs
                time (torch tensor, float), dims: (1)
                seasonalities (OrderedDict), named seasonalities, each with features
                    (torch tensor, float) of dims: (n_features[name])
                lags (torch tensor, float), dims: (n_lags)
                covariates (OrderedDict), named covariates, each with features
                    (np.array, float) of dims: (n_lags)
                events (np.array), all event features of dims: (n_event_params)
            targets (torch tensor, float): targets to be predicted, dims: (n_forecasts)
        """
        # Future TODO: vectorize
        sample = OrderedDict({})
        for key, data in self.inputs.items():
            if key in self.two_level_inputs:
                sample[key] = OrderedDict({})
                for name, period_features in self.inputs[key].items():
                    sample[key][name] = period_features[index]
            elif key == "events":
                sample[key] = OrderedDict({})
                for mode, event_features in self.inputs[key].items():
                    sample[key][mode] = event_features[index, :, :]
            else:
                sample[key] = data[index]
        targets = self.targets[index]
        return sample, targets

    def __len__(self):
        """Overrides Parent class method to get data length."""
        return self.length


def tabularize_univariate_datetime(
        df,
        season_config=None,
        n_lags=0,
        n_forecasts=1,
        events_config=None,
        country_holidays_config=None,
        covar_config=None,
        predict_mode=False,
        verbose=False,
):
    """Create a tabular dataset from univariate timeseries for supervised forecasting.

    Note: data must be clean and have no gaps.

    Args:
        df (pd.DataFrame): Sequence of observations
            with original 'ds', 'y' and normalized 't', 'y_scaled' columns.
        season_config (AttrDict): configuration for seasonalities.
        n_lags (int): number of lagged values of series to include as model inputs. Aka AR-order
        n_forecasts (int): number of steps to forecast into future.
        events_config (OrderedDict): user specified events, each with their
            upper, lower windows (int) and regularization
        country_holidays_config (OrderedDict): Configurations (holiday_names, upper, lower windows,
            regularization) for country specific holidays
        covar_config (OrderedDict): configuration for covariates
        predict_mode (bool): False (default) includes target values.
            True does not include targets but includes entire dataset as input
        verbose (bool): whether to print status updates

    Returns:
        inputs (OrderedDict): model inputs, each of len(df) but with varying dimensions
            time (np.array, float), dims: (num_samples, 1)
            seasonalities (OrderedDict), named seasonalities, each with features
                (np.array, float) of dims: (num_samples, n_features[name])
            lags (np.array, float), dims: (num_samples, n_lags)
            covariates (OrderedDict), named covariates, each with features
                (np.array, float) of dims: (num_samples, n_lags)
            events (np.array), all event features of dims: (num_samples, n_event_params)
        targets (np.array, float): targets to be predicted of same length as each of the model inputs,
            dims: (num_samples, n_forecasts)
    """
    n_samples = len(df) - n_lags + 1 - n_forecasts
    # data is stored in OrderedDict
    inputs = OrderedDict({})

    def _stride_time_features_for_forecasts(x):
        # only for case where n_lags > 0
        return np.array([x[n_lags + i: n_lags + i + n_forecasts] for i in range(n_samples)])

    # time is the time at each forecast step
    t = df.loc[:, 't'].values
    if n_lags == 0:
        assert n_forecasts == 1
        time = np.expand_dims(t, 1)
    else:
        time = _stride_time_features_for_forecasts(t)
    inputs["time"] = time

    if season_config is not None:
        seasonalities = seasonal_features_from_dates(df['ds'], season_config)
        for name, features in seasonalities.items():
            if n_lags == 0:
                seasonalities[name] = np.expand_dims(features, axis=1)
            else:
                # stride into num_forecast at dim=1 for each sample, just like we did with time
                seasonalities[name] = _stride_time_features_for_forecasts(features)
        inputs["seasonalities"] = seasonalities

    def _stride_lagged_features(df_col_name, feature_dims):
        # only for case where n_lags > 0
        series = df.loc[:, df_col_name].values
        return np.array([series[i + n_lags - feature_dims: i + n_lags] for i in range(n_samples)])

    if n_lags > 0 and 'y' in df.columns:
        inputs["lags"] = _stride_lagged_features(df_col_name='y_scaled', feature_dims=n_lags)
        if np.isnan(inputs["lags"]).any():
            raise ValueError('Input lags contain NaN values in y.')

    if covar_config is not None and n_lags > 0:
        covariates = OrderedDict({})
        for covar in df.columns:
            if covar in covar_config:
                assert n_lags > 0
                window = n_lags
                if covar_config[covar].as_scalar: window = 1
                covariates[covar] = _stride_lagged_features(df_col_name=covar, feature_dims=window)
                if np.isnan(covariates[covar]).any():
                    raise ValueError('Input lags contain NaN values in ', covar)

        inputs['covariates'] = covariates

    # get the events features
    if events_config is not None or country_holidays_config is not None:
        additive_events, multiplicative_events = make_events_features(df, events_config, country_holidays_config)

        events = OrderedDict({})
        if n_lags == 0:
            if additive_events is not None:
                events["additive"] = np.expand_dims(additive_events, axis=1)
            if multiplicative_events is not None:
                events["multiplicative"] = np.expand_dims(multiplicative_events, axis=1)
        else:
            if additive_events is not None:
                additive_event_feature_windows = []
                for i in range(0, additive_events.shape[1]):
                    # stride into num_forecast at dim=1 for each sample, just like we did with time
                    additive_event_feature_windows.append(_stride_time_features_for_forecasts(additive_events[:, i]))
                additive_events = np.dstack(additive_event_feature_windows)
                events["additive"] = additive_events

            if multiplicative_events is not None:
                multiplicative_event_feature_windows = []
                for i in range(0, multiplicative_events.shape[1]):
                    # stride into num_forecast at dim=1 for each sample, just like we did with time
                    multiplicative_event_feature_windows.append(
                        _stride_time_features_for_forecasts(multiplicative_events[:, i]))
                multiplicative_events = np.dstack(multiplicative_event_feature_windows)
                events["multiplicative"] = multiplicative_events

        inputs["events"] = events

    if predict_mode:
        targets = np.empty_like(time)
    else:
        targets = _stride_time_features_for_forecasts(df['y_scaled'].values)

    if verbose:
        print("Tabularized inputs shapes:")
        for key, value in inputs.items():
            if key in ["seasonalities", "covariates", "events"]:
                for name, period_features in value.items():
                    print("".join([" "] * 4), name, key, period_features.shape)
            else:
                print("".join([" "] * 4), key, value.shape)
    return inputs, targets


def fourier_series(dates, period, series_order):
    """Provides Fourier series components with the specified frequency and order.

    Note: Identical to OG Prophet.

    Args:
        dates (pd.Series): containing timestamps.
        period (float): Number of days of the period.
        series_order (int): Number of fourier components.

    Returns:
        Matrix with seasonality features.
    """
    # convert to days since epoch
    t = np.array(
        (dates - datetime(1970, 1, 1))
            .dt.total_seconds()
            .astype(np.float)
    ) / (3600 * 24.)
    features = np.column_stack(
        [fun((2.0 * (i + 1) * np.pi * t / period))
         for i in range(series_order)
         for fun in (np.sin, np.cos)
         ])
    return features


def make_country_specific_holidays_df(year_list, country):
    """
    Make dataframe of country specific holidays for given years and countries

    Args:
        year_list (list): a list of years
        country (string): country name

    Returns:
        pd.DataFrame with 'ds' and 'holiday'.
    """

    try:
        country_specific_holidays = getattr(hdays_part2, country)(years=year_list)
    except AttributeError:
        try:
            country_specific_holidays = getattr(hdays_part1, country)(years=year_list)
        except AttributeError:
            raise AttributeError(
                "Holidays in {} are not currently supported!".format(country))
    country_specific_holidays_dict = defaultdict(list)
    for date, holiday in country_specific_holidays.items():
        country_specific_holidays_dict[holiday].append(pd.to_datetime(date))
    return country_specific_holidays_dict


def make_events_features(df, events_config=None, country_holidays_config=None):
    """
    Construct array of all event features

    Args:
        df (pd.DataFrame): dataframe with all values including the user specified events (provided by user)
        events_config (OrderedDict): user specified events, each with their
            upper, lower windows (int), regularization
        country_holidays_config (OrderedDict): Configurations (holiday_names, upper, lower windows, regularization)
            for country specific holidays

    Returns:
        additive_events (np.array): all additive event features (both user specified and country specific)
        multiplicative_events (np.array): all multiplicative event features (both user specified and country specific)
    """

    additive_events = pd.DataFrame()
    multiplicative_events = pd.DataFrame()

    # create all user specified events
    if events_config is not None:
        for event, configs in events_config.items():
            if event not in df.columns:
                df[event] = 0.
            feature = df[event]
            lw = configs.lower_window
            uw = configs.upper_window
            mode = configs["mode"]
            # create lower and upper window features
            for offset in range(lw, uw + 1):
                key = utils.create_event_names_for_offsets(event, offset)
                offset_feature = feature.shift(periods=offset, fill_value=0)
                if mode == "additive":
                    additive_events[key] = offset_feature
                else:
                    multiplicative_events[key] = offset_feature

    # create all country specific holidays
    if country_holidays_config is not None:
        lw = country_holidays_config["lower_window"]
        uw = country_holidays_config["upper_window"]
        mode = country_holidays_config["mode"]
        year_list = list({x.year for x in df.ds})
        country_holidays_dict = make_country_specific_holidays_df(year_list, country_holidays_config["country"])
        for holiday in country_holidays_config["holiday_names"]:
            feature = pd.Series([0.] * df.shape[0])
            if holiday in country_holidays_dict.keys():
                dates = country_holidays_dict[holiday]
                feature[df.ds.isin(dates)] = 1.
            for offset in range(lw, uw + 1):
                key = utils.create_event_names_for_offsets(holiday, offset)
                offset_feature = feature.shift(periods=offset, fill_value=0)
                if mode == "additive":
                    additive_events[key] = offset_feature
                else:
                    multiplicative_events[key] = offset_feature

    # Make sure column order is consistent
    if not additive_events.empty:
        additive_events = additive_events[sorted(additive_events.columns.tolist())]
        additive_events = additive_events.values
    else:
        additive_events = None
    if not multiplicative_events.empty:
        multiplicative_events = multiplicative_events[sorted(multiplicative_events.columns.tolist())]
        multiplicative_events = multiplicative_events.values
    else:
        multiplicative_events = None

    return additive_events, multiplicative_events


def seasonal_features_from_dates(dates, season_config):
    """Dataframe with seasonality features.

    Includes seasonality features, holiday features, and added regressors.

    Args:
        dates (pd.Series): with dates for computing seasonality features
        season_config (AttrDict): configuration from NeuralProphet

    Returns:
         Dictionary with keys for each period name containing an np.array with the respective regression features.
            each with dims: (len(dates), 2*fourier_order)
    """
    assert len(dates.shape) == 1
    seasonalities = OrderedDict({})
    # Seasonality features
    for name, period in season_config.periods.items():
        if period['resolution'] > 0:
            if season_config.type == 'fourier':
                features = fourier_series(
                    dates=dates,
                    period=period['period'],
                    series_order=period['resolution'],
                )
            else:
                raise NotImplementedError
            seasonalities[name] = features

    return seasonalities


def test(verbose=True):
    # Might not be up to date
    data_path = os.path.join(os.getcwd(), 'data')
    # data_path = os.path.join(os.path.dirname(os.getcwd()), 'data')
    data_name = 'example_air_passengers.csv'

    ## manually load any file that stores a time series, for example:
    df_in = pd.read_csv(os.path.join(data_path, data_name), index_col=False)
    # df_in['extra'] = df_in['y'].rolling(7, min_periods=1).mean()
    if verbose:
        print(df_in.shape)

    n_lags = 3
    n_forecasts = 1
    valid_p = 0.2
    df_train, df_val = split_df(df_in, n_lags, n_forecasts, valid_p, inputs_overbleed=True, verbose=verbose)

    ## create a tabularized dataset from time series
    df = check_dataframe(df_train)
    data_params = init_data_params(df)
    df = df_utils.normalize(df, data_params)
    inputs, targets = tabularize_univariate_datetime(
        df,
        n_lags=n_lags,
        n_forecasts=n_forecasts,
        verbose=verbose,
    )
    if verbose:
        print("tabularized inputs")
        for inp, values in inputs.items():
            print(inp, values.shape)
        print("targets", targets.shape)


if __name__ == '__main__':
    test()
