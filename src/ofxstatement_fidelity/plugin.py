from __future__ import annotations

from os import path

from decimal import Decimal, Decimal as D
from datetime import datetime
import re
from typing import Any, TextIO, get_args, get_origin

from ofxstatement.plugin import Plugin
from ofxstatement.parser import AbstractStatementParser
from ofxstatement.statement import Statement, InvestStatementLine

import logging
import csv

LOGGER = logging.getLogger(__name__)


class FidelityPlugin(Plugin):
    """Sample plugin (for developers only)"""

    def get_parser(self, filename: str) -> FidelityCSVParser:
        parser = FidelityCSVParser(filename)
        return parser


class FidelityCSVParser(AbstractStatementParser):
    statement: Statement
    fin: TextIO  # file input stream
    # 0-based csv column mapping to StatementLine field

    date_format: str = "%Y-%m-%d"
    cur_record: int = 0

    def __init__(self, filename: str) -> None:
        super().__init__()
        self.filename = filename
        self.statement = Statement()
        self.statement.broker_id = "fidelity.com"
        self.statement.currency = "USD"
        self.id_generator = IdGenerator()
        self.account_number: str | None = None
        self.column_map: dict[str, int] = {}
        self.end_cash_balance: D | None = None

    def parse_datetime(self, value: str) -> datetime:
        return datetime.strptime(value, self.date_format)

    def parse_decimal(self, value: str | None) -> D | None:
        if value is None:
            return None

        cleaned = value.strip()
        if cleaned in ("", "--"):
            return None

        # some plugins pass localised numbers, clean them up
        return D(cleaned.replace(",", ".").replace(" ", ""))

    def parse_value(self, value: str | None, field: str) -> Any:
        tp = InvestStatementLine.__annotations__.get(field)
        if value is None:
            return None

        def _matches(target) -> bool:
            if tp is target:
                return True
            origin = get_origin(tp)
            if origin is None:
                return False
            return target in get_args(tp)

        if _matches(datetime):
            return self.parse_datetime(value)
        if _matches(Decimal):
            return self.parse_decimal(value)
        return value

    def parse_record(self, line):
        """Parse given transaction line and return StatementLine object"""

        invest_stmt_line = InvestStatementLine()

        cleaned_line = list(line)

        if cleaned_line and isinstance(cleaned_line[0], str):
            cleaned_line[0] = cleaned_line[0].lstrip("\ufeff").strip()

        cleaned_line = [
            col.strip() if isinstance(col, str) else col for col in cleaned_line
        ]

        # tolerate the BOM-only line and blank lines
        if len(cleaned_line) == 0 or (len(cleaned_line) == 1 and cleaned_line[0] == ""):
            return None

        # skip the header and capture column order
        if cleaned_line[0] == "Run Date":
            self.column_map = {col: idx for idx, col in enumerate(cleaned_line) if col}
            return None

        if not self.column_map:
            return None

        def column_value(name: str) -> str | None:
            idx = self.column_map.get(name)
            if idx is None or idx >= len(cleaned_line):
                return None
            return cleaned_line[idx]

        run_date = column_value("Run Date")
        account_number = column_value("Account Number")
        action = column_value("Action")
        symbol = column_value("Symbol")
        # description = column_value("Description")
        # type_field = column_value("Type")
        price = column_value("Price ($)")
        quantity = column_value("Quantity")
        # commission = column_value("Commission ($)")
        fees = column_value("Fees ($)")
        # accrued_interest = column_value("Accrued Interest ($)")
        amount = column_value("Amount ($)")
        cash_balance = column_value("Cash Balance ($)")
        settlement_date = column_value("Settlement Date")

        if run_date is None or action is None:
            return None

        # skip lines which are comments
        if run_date[:1] == '"':
            return None

        # skip any line that does not begin with a digit
        if not run_date[:1].isdigit():
            return None

        if not amount or float(amount) == 0:
            return None

        if account_number:
            self.account_number = account_number

        invest_stmt_line.memo = action

        invest_stmt_line.fees = self.parse_value(fees, "fees")
        invest_stmt_line.amount = amount_value = self.parse_value(amount, "amount")

        if (
            isinstance(cash_balance, str)
            and cash_balance.strip().lower() == "processing"
        ):
            return None

        cash_balance_value = self.parse_decimal(cash_balance)
        if cash_balance_value is not None and self.end_cash_balance is None:
            self.end_cash_balance = cash_balance_value

        def parse_us_date(date_str: str) -> datetime:
            for fmt in ("%m/%d/%Y", "%m/%d/%y"):
                try:
                    return datetime.strptime(date_str[:10], fmt)
                except ValueError:
                    continue
            raise ValueError(f"Unrecognized date format: {date_str}")

        date = parse_us_date(run_date)
        invest_stmt_line.date = date
        id = self.id_generator.create_id(date)
        invest_stmt_line.id = id

        if settlement_date:
            date_user = parse_us_date(settlement_date)
        else:
            date_user = date

        invest_stmt_line.date_user = date_user

        quantity_value = self.parse_decimal(quantity)
        price_value = self.parse_decimal(price)

        if quantity_value is not None:
            invest_stmt_line.units = quantity_value
        if price_value is not None:
            invest_stmt_line.unit_price = price_value
        if symbol:
            invest_stmt_line.security_id = symbol

        def set_buy(trntype_detailed: str) -> None:
            invest_stmt_line.trntype = "BUYSTOCK"
            invest_stmt_line.trntype_detailed = trntype_detailed
            invest_stmt_line.units = quantity_value
            invest_stmt_line.unit_price = price_value

        def set_sell(trntype_detailed: str) -> None:
            invest_stmt_line.trntype = "SELLSTOCK"
            invest_stmt_line.trntype_detailed = trntype_detailed
            invest_stmt_line.units = quantity_value
            invest_stmt_line.unit_price = price_value

        def set_income(trntype_detailed: str) -> None:
            invest_stmt_line.trntype = "INCOME"
            invest_stmt_line.trntype_detailed = trntype_detailed

        def set_expense() -> None:
            invest_stmt_line.trntype = "INVEXPENSE"
            invest_stmt_line.trntype_detailed = None

        def set_banktran(detail: str) -> None:
            invest_stmt_line.trntype = "INVBANKTRAN"
            invest_stmt_line.trntype_detailed = detail

        if re.match(r"^REINVESTMENT .*(Cash)", action):
            # REINVESTMENT FIDELITY GOVERNMENT MONEY MARKET (SPAXX) (Cash) should be ignored
            return None
        elif re.match(r"^DIVIDEND RECEIVED ", action):
            set_income("DIV")
            invest_stmt_line.units = quantity_value
            invest_stmt_line.unit_price = price_value
        elif re.match(r"^YOU BOUGHT ", action):
            set_buy("BUY")
        elif re.match(r"^YOU SOLD ", action):
            set_sell("SELL")
        elif re.match(r"^TAX WITHHELD ", action):
            if symbol and amount_value is not None:
                set_expense()
            else:
                set_banktran("DEBIT")
        elif re.match(r"^INTEREST EARNED ", action):
            set_banktran("INT")
        elif re.match(r"^DIRECT DEBIT ", action):
            set_banktran("DEBIT")
        elif re.match(r"^Electronic Funds Transfer Paid ", action):
            set_banktran("DEBIT")
        elif re.match(r"^Check Paid", action):
            set_banktran("DEBIT")
        elif re.match(r"^DEBIT CARD PURCHASE", action):
            set_banktran("DEBIT")
        elif re.match(r"^REDEMPTION FROM CORE ACCOUNT", action):
            set_sell("SELL")
        elif re.match(r"^TRANSFERRED FROM ", action):
            set_banktran("XFER")
        elif re.match(r"^DIRECT DEPOSIT ", action):
            set_banktran("CREDIT")
        elif re.match(r"^Contributions$", action):
            set_banktran("CREDIT")
        elif re.match(r"^WIRE TRANSFER FROM ", action):
            set_banktran("XFER")
        elif re.match(r"^WIRE TRANSFER TO ", action):
            set_banktran("XFER")
        elif re.match(r"^Electronic Funds Transfer Received", action):
            set_banktran("CREDIT")
        elif re.match(r"^TRANSFER OF ASSETS ACAT RECEIVE", action):
            set_banktran("XFER")
        elif re.match(r"^TRANSFER OF ASSETS ACAT DELIVER", action):
            set_banktran("XFER")
        elif re.match(r"^TRANSFERRED TO ", action):
            set_banktran("XFER")
        elif re.match(r"^JOURNALED", action):
            set_banktran("OTHER")
        else:
            if isinstance(amount_value, Decimal):
                set_banktran("CREDIT" if amount_value >= 0 else "DEBIT")
            else:
                set_banktran("OTHER")
            LOGGER.warning(
                f"Unknown action: {action}.  Guess it is: {invest_stmt_line}"
            )

        return invest_stmt_line

    # parse the CSV file and return a Statement
    def parse(self) -> Statement:
        """Main entry point for parsers"""
        with open(self.filename, "r") as fin:
            self.fin = fin

            reader = csv.reader(self.fin)

            # loop through the CSV file lines
            for csv_line in reader:
                self.cur_record += 1
                if not csv_line:
                    continue
                invest_stmt_line = self.parse_record(csv_line)
                if invest_stmt_line:
                    invest_stmt_line.assert_valid()
                    self.statement.invest_lines.append(invest_stmt_line)

            # derive account id from file name
            match = re.search(r".*Account_(\w*).*\.csv", path.basename(self.filename))
            if match and match[1]:
                self.statement.account_id = match[1]
            elif self.account_number:
                self.statement.account_id = self.account_number
            else:
                LOGGER.warning(
                    f"Unable to derive account id from {self.filename}, please use a filename like xxx_Account_123456789.csv"
                )

            # reverse the lines
            self.statement.invest_lines.reverse()

            # after reversing the lines in the list, update the id
            for invest_line in self.statement.invest_lines:
                date = invest_line.date
                new_id = self.id_generator.create_id(date)
                invest_line.id = new_id

            if self.statement.invest_lines:
                # figure out start_date and end_date for the statement
                self.statement.start_date = min(
                    sl.date for sl in self.statement.invest_lines if sl.date is not None
                )
                self.statement.end_date = max(
                    sl.date for sl in self.statement.invest_lines if sl.date is not None
                )
                self.statement.end_balance = self.end_cash_balance

            # print(f"{self.statement}")
            return self.statement


##########################################################################
class IdGenerator:
    """Generates a unique ID based on the date

    Hopefully any JSON file that we get will have all the transactions for a
    given date, and hopefully in the same order each time so that these IDs
    will match up across exports.
    """

    def __init__(self) -> None:
        self.date_count: dict[datetime, int] = {}

    def create_id(self, date) -> str:
        self.date_count[date] = self.date_count.get(date, 0) + 1
        return f"{datetime.strftime(date, '%Y%m%d')}-{self.date_count[date]}"
