from __future__ import annotations

from os import path

from decimal import Decimal, Decimal as D
from datetime import datetime
import re
from typing import Any, TextIO, get_args, get_origin

from ofxstatement.plugin import Plugin
from ofxstatement.parser import AbstractStatementParser
from ofxstatement.statement import Statement, InvestStatementLine, StatementLine

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
        self.statement.broker_id = "Fidelity"
        self.statement.currency = "USD"
        self.id_generator = IdGenerator()
        self.account_numbers: set[str] = set()

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
        tp = StatementLine.__annotations__.get(field)
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

        # line[0 ] : Run Date
        # line[1 ] : Account (only in Accounts_History)
        # line[2 ] : Account Number (only in Accounts_History)
        # line[3 ] : Action
        # line[4 ] : Symbol
        # line[5 ] : Description
        # line[6 ] : Type
        # line[7 ] : Price ($)
        # line[8 ] : Quantity
        # line[9 ] : Commission ($)
        # line[10] : Fees ($)
        # line[11] : Accrued Interest ($)
        # line[12] : Amount ($)
        # line[13] : Settlement Date

        line_length = len(cleaned_line)

        if line_length > 14:
            cleaned_line = cleaned_line[:14]
            line_length = len(cleaned_line)

        # tolerate the BOM-only line and blank lines
        if line_length == 0 or (line_length == 1 and cleaned_line[0] == ""):
            return None

        # skip the header
        if cleaned_line[0] == "Run Date":
            return None

        # skip lines which are comments
        if cleaned_line[0][:1] == '"':
            return None

        # skip any line that does not begin with a digit
        if not cleaned_line[0][:1].isdigit():
            return None

        if line_length == 13:
            (
                run_date,
                action,
                symbol,
                description,
                type_field,
                quantity,
                price,
                commission,
                fees,
                accrued_interest,
                amount,
                cash_balance,
                settlement_date,
            ) = cleaned_line
            account = None
            account_number = None
        elif line_length >= 14:
            # Accounts_History exports sometimes include a trailing empty column; drop extras
            normalized = cleaned_line[:14]
            (
                run_date,
                account,
                account_number,
                action,
                symbol,
                description,
                type_field,
                price,
                quantity,
                commission,
                fees,
                accrued_interest,
                amount,
                settlement_date,
            ) = normalized
        else:
            return None

        if account_number:
            self.account_numbers.add(account_number)

        invest_stmt_line.memo = action

        # fees
        field = "fees"
        value = self.parse_value(fees, field)
        setattr(invest_stmt_line, field, value)

        # amount
        field = "amount"
        value = self.parse_value(amount, field)
        setattr(invest_stmt_line, field, value)

        date = datetime.strptime(run_date[0:10], "%m/%d/%Y")
        invest_stmt_line.date = date
        id = self.id_generator.create_id(date)
        invest_stmt_line.id = id

        if settlement_date:
            date_user = datetime.strptime(settlement_date[0:10], "%m/%d/%Y")
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

        if re.match(r"^REINVESTMENT ", action):
            invest_stmt_line.trntype = "BUYSTOCK"
            invest_stmt_line.trntype_detailed = "BUY"
            invest_stmt_line.security_id = symbol
            invest_stmt_line.units = quantity_value
            invest_stmt_line.unit_price = price_value
        elif re.match(r"^DIVIDEND RECEIVED ", action):
            invest_stmt_line.trntype = "INCOME"
            invest_stmt_line.trntype_detailed = "DIV"
            invest_stmt_line.security_id = symbol
        elif re.match(r"^YOU BOUGHT ", action):
            invest_stmt_line.trntype = "BUYSTOCK"
            invest_stmt_line.trntype_detailed = "BUY"
            invest_stmt_line.security_id = symbol
            invest_stmt_line.units = quantity_value
            invest_stmt_line.unit_price = price_value
        elif re.match(r"^YOU SOLD ", action):
            invest_stmt_line.trntype = "SELLSTOCK"
            invest_stmt_line.trntype_detailed = "SELL"
            invest_stmt_line.security_id = symbol
            invest_stmt_line.units = quantity_value
            invest_stmt_line.unit_price = price_value
        elif re.match(r"^DIRECT DEBIT ", action):
            invest_stmt_line.trntype = "INVBANKTRAN"
            invest_stmt_line.trntype_detailed = "DEBIT"
        elif re.match(r"^Electronic Funds Transfer Paid ", action):
            invest_stmt_line.trntype = "INVBANKTRAN"
            invest_stmt_line.trntype_detailed = "DEBIT"
        elif re.match(r"^TRANSFERRED FROM ", action):
            invest_stmt_line.trntype = "INVBANKTRAN"
            invest_stmt_line.trntype_detailed = "CREDIT"
        elif re.match(r"^DIRECT DEPOSIT ", action):
            invest_stmt_line.trntype = "INVBANKTRAN"
            invest_stmt_line.trntype_detailed = "CREDIT"
        elif re.match(r"^INTEREST EARNED ", action):
            invest_stmt_line.trntype = "INVBANKTRAN"
            invest_stmt_line.trntype_detailed = "CREDIT"
        elif re.match(r"^TAX WITHHELD ", action):
            invest_stmt_line.trntype = "INVBANKTRAN"
            invest_stmt_line.trntype_detailed = "DEBIT"
        elif re.match(r"^Contributions$", action):
            invest_stmt_line.trntype = "INVBANKTRAN"
            invest_stmt_line.trntype_detailed = "CREDIT"
        else:
            raise ValueError(f"Unknown action: {action}")

        # print(f"{invest_stmt_line}")
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
            match = re.search(
                r".*History_for_Account_(.*)\.csv", path.basename(self.filename)
            )
            if match:
                self.statement.account_id = match[1]
            elif len(self.account_numbers) == 1:
                # Accounts_History exports include a single account number in column 3
                self.statement.account_id = next(iter(self.account_numbers))

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
