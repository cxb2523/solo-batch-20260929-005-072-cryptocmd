# cryptoCMD: cryptoCurrency Market Data

[![PyPI Version][]][1] [![CI Status][]][2] [![License][]][3] [![Downloads][]][4] [![Ruff][]][5] [![GitHub Sponsors][]][6]


Cryptocurrency historical market price data scraper written in Python.

## Installation

```sh
pip install cryptocmd
```

to install from the latest source use following command

```sh
pip install git+git://github.com/guptarohit/cryptoCMD.git
```

## Usage

### CoinMarketCap Scraper

Following methods are available to get data in multiple formats from
<https://coinmarketcap.com>

#### To get all time historical data of a cryptocurrency

```python
from cryptocmd import CmcScraper

# initialise scraper without time interval
scraper = CmcScraper("XRP")

# get raw data as list of list
headers, data = scraper.get_data()

# get data in a json format
xrp_json_data = scraper.get_data("json")

# export the data as csv file, you can also pass optional `name` parameter
scraper.export("csv", name="xrp_all_time")

# Pandas dataFrame for the same data
df = scraper.get_dataframe()
```

#### To get data of a cryptocurrency which have same coin code as others

```python
from cryptocmd import CmcScraper

# initialise scraper with coin name as well
scraper = CmcScraper(coin_code="sol", coin_name="solana")

# get raw data as list of list
headers, data = scraper.get_data()

# get data in a json format
solana_json_data = scraper.get_data("json")

# export the data as csv file, you can also pass optional `name` parameter
scraper.export("csv", name="solana_all_time")

# Pandas dataFrame for the same data
df = scraper.get_dataframe()
```

#### To get data of a cryptocurrency for some days

```python
from cryptocmd import CmcScraper

# initialise scraper with time interval
scraper = CmcScraper("XRP", "15-10-2017", "25-10-2017")

# get raw data as list of list
headers, data = scraper.get_data()

# get data in a json format
json_data = scraper.get_data("json")

# export the data to csv
scraper.export("csv")

# get dataframe for the data
df = scraper.get_dataframe()
```

##### Following are the columns of the data

`Date, Open, High, Low, Close, Volume, Market Cap, Time Open, Time High, Time Low, Time Close`
## Exchange backends (dispatch)

Data fetching is decoupled from CoinMarketCap and dispatched per exchange:
`cryptocmd` only selects a backend by name, and each exchange lives in its own
module under `cryptocmd/exchanges/`. Adding an exchange means adding one file —
the package core never changes.

```python
from cryptocmd import get_scraper

# same CmcScraper-compatible surface for every backend
scraper = get_scraper("bitstamp", "BTC", "15-12-2023", "15-01-2024", fiat="USD")
headers, rows = scraper.get_data()
scraper.export("csv", name="btc_bitstamp")

# the legacy CMC path is the default and remains unchanged
cmc = get_scraper("cmc", "BTC")          # equivalent to CmcScraper("BTC")
```

Built-in backends: `cmc` / `coinmarketcap` (legacy, default) and `bitstamp`.
All backends emit the exact same columns, `Date` index and CSV bytes as CMC.

Pipeline guarantees:

- ranges wider than a single request are sliced on **UTC day boundaries** with
  no gap or overlap;
- HTTP `429` responses use **exponential backoff**;
- each daily candle is persisted idempotently (one file per UTC day), so a
  re-fetch never writes the same candle twice;
- missing days / trading halts follow one pipeline-wide policy selected with
  `CRYPTOCMD_MISSING_POLICY`: `nan` (default, retain NaN rows) or `ffill`
  (forward fill). There is deliberately no other option.

### Registering a third-party backend

Either register at runtime:

```python
from cryptocmd import register_backend, Backend
register_backend("myexchange", Backend("myexchange", MyScraper))
```

…or declare the `cryptocmd.exchanges` entry point in your package metadata.

## Local server

`cryptocmd/server.py` serves OHLCV locally:

- `GET /health` → `ok`
- `GET /ohlcv?exchange=bitstamp&coin=btc&fiat=usd&range=30d` → CSV (or
  `format=json`; `range` is one of `7d`, `30d`, `90d`, `all`, or pass explicit
  `start`/`end` as `dd-mm-yyyy`)
- `GET /` → a dashboard with range switching and a missing-point counter

```sh
python -m cryptocmd.server --fixtures        # offline, deterministic (tests)
python -m cryptocmd.server --exchange bitstamp
```

The acceptance gate is `scripts/verify.sh`: it checks `/health`, byte-diffs
`/ohlcv` against the frozen baseline CSV, and runs the regression suite.

## Acknowledgements

The data is being scrapped from
[coinmarketcap](https://coinmarketcap.com) :v: and it\'s
[free](https://coinmarketcap.com/faq/) to use. :tada:

## Contributing

Feel free to make a pull request! :octocat:

If you found this useful, I\'d appreciate your consideration in the
below. ✨☕


[PyPI Version]: https://img.shields.io/pypi/v/cryptoCMD.svg
[1]: https://pypi.python.org/pypi/cryptoCMD
[CI Status]: https://github.com/guptarohit/cryptoCMD/actions/workflows/ci.yml/badge.svg
[2]: https://github.com/guptarohit/cryptoCMD/actions/workflows/ci.yml
[License]: https://img.shields.io/pypi/l/cryptoCMD.svg
[3]: https://github.com/guptarohit/cryptoCMD/blob/master/LICENSE
[Downloads]: https://pepy.tech/badge/cryptoCMD
[4]: https://pepy.tech/project/cryptoCMD
[Ruff]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json
[5]: https://github.com/astral-sh/ruff
[GitHub Sponsors]: https://img.shields.io/github/sponsors/guptarohit?color=%23FF5733
[6]: https://github.com/sponsors/guptarohit
[![Buy Me A Coffee](https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20coffee&emoji=&slug=rohitgupta&button_colour=5F7FFF&font_colour=ffffff&font_family=Lato&outline_colour=000000&coffee_colour=FFDD00)](https://www.buymeacoffee.com/rohitgupta)
