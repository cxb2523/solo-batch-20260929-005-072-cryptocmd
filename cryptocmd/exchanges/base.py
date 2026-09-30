"""Base scraper with the CmcScraper-compatible output surface."""

from __future__ import annotations

from datetime import datetime
import os
import warnings

import tablib

from ..utils import InvalidParameters
from .common import HEADERS, rows_to_csv_bytes


class ExchangeScraper:
    """Common scraper contract mirroring :class:`cryptocmd.core.CmcScraper`.

    Subclasses implement :meth:`_fetch_rows` which populates ``self.rows`` with
    lists ordered exactly like :data:`cryptocmd.exchanges.common.HEADERS`. The
    headers, ordering, CSV dialect and DataFrame/index shape are identical to
    the CMC path so downstream consumers cannot tell the backends apart.
    """

    def __init__(
        self,
        coin_code=None,
        start_date=None,
        end_date=None,
        all_time=False,
        order_ascending=False,
        fiat="USD",
        coin_name=None,
        id_number=None,
        cache_dir=None,
    ):
        self.coin_code = coin_code
        self.start_date = start_date
        self.end_date = end_date
        self.all_time = bool(all_time)
        self.order_ascending = order_ascending
        self.fiat = fiat
        self.coin_name = coin_name
        self.id_number = id_number
        self.cache_dir = cache_dir
        self.headers = list(HEADERS)
        self.rows = []

        if not (self.start_date and self.end_date):
            self.all_time = True

        if not (self.all_time or (self.start_date and self.end_date)):
            raise InvalidParameters(
                "'start_date' or 'end_date' cannot be empty if 'all_time' flag is False"
            )

    def _fetch_rows(self):
        """Return raw rows for the configured range. Override in subclasses."""
        raise NotImplementedError

    def _download_data(self, **kwargs):
        forced = kwargs.get("forced")
        if self.rows and not forced:
            return
        if self.all_time:
            self.start_date, self.end_date = None, None

        self.rows = self._fetch_rows()

        if self.rows:
            self.end_date, self.start_date = self.rows[0][0], self.rows[-1][0]

        if self.order_ascending:
            self.rows.sort(key=lambda row: datetime.strptime(row[0], "%d-%m-%Y"))

    def get_data(self, format="", verbose=False, **kwargs):
        self._download_data(**kwargs)
        if verbose:
            print(*self.headers, sep=", ")
            for row in self.rows:
                print(*row, sep=", ")
        elif format:
            data = tablib.Dataset()
            data.headers = self.headers
            for row in self.rows:
                data.append(row)
            return data.export(format)
        return self.headers, self.rows

    def get_dataframe(self, date_as_index=False, **kwargs):
        try:
            import pandas as pd
        except ImportError:
            pd = None
        if pd is None:
            raise NotImplementedError(
                "DataFrame Format requires 'pandas' to be installed."
                "Try : pip install pandas"
            )

        self._download_data(**kwargs)
        dataframe = pd.DataFrame(data=self.rows, columns=self.headers)
        dataframe["Date"] = pd.to_datetime(
            dataframe["Date"], format="%d-%m-%Y", dayfirst=True
        )
        if date_as_index:
            dataframe.set_index("Date", inplace=True)
        return dataframe

    def export_csv(self, csv_name=None, csv_path=None, **kwargs):
        warnings.warn(
            "export_csv will be deprecated; Use 'export' method instead, e.g. export('csv')",
            PendingDeprecationWarning,
            stacklevel=2,
        )
        self._download_data(**kwargs)
        if csv_path is None:
            csv_path = os.getcwd()
        if csv_name is None:
            csv_name = (
                f"{self.coin_code}_{self.fiat}_{self.start_date}_{self.end_date}.csv"
            )
        if not csv_name.endswith(".csv"):
            csv_name += ".csv"
        target = os.path.join(csv_path, csv_name)
        with open(target, "wb") as handle:
            handle.write(rows_to_csv_bytes(self.headers, self.rows))

    def export(self, format, name=None, path=None, **kwargs):
        data = self.get_data(format, **kwargs)
        if path is None:
            path = os.getcwd()
        if name is None:
            name = f"{self.coin_code}_{self.fiat}-{self.start_date}_{self.end_date}"
        if not name.endswith(f".{format}"):
            name += f".{format}"
        target = os.path.join(path, name)
        with open(target, "wb") as handle:
            if isinstance(data, str):
                handle.write(data.encode("utf-8"))
            else:
                handle.write(data)

    def to_csv_bytes(self, **kwargs):
        """Return the exact byte stream ``export('csv')`` writes (tablib)."""
        return self.get_data("csv", **kwargs).encode("utf-8")
