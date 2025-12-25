import os
from datetime import datetime
from decimal import Decimal
from textwrap import dedent

from ofxstatement.ui import UI

from ofxstatement_fidelity.plugin import FidelityPlugin


def test_fidelity() -> None:
    plugin = FidelityPlugin(UI(), {})
    here = os.path.dirname(__file__)
    fidelity_filename = os.path.join(here, "History_for_Account_2TB000009.csv")

    parser = plugin.get_parser(fidelity_filename)
    statement = parser.parse()

    assert statement is not None


def test_skips_processing_cash_balance_row(tmp_path) -> None:
    plugin = FidelityPlugin(UI(), {})
    csv_path = tmp_path / "History_for_Account_123.csv"
    csv_path.write_text(
        dedent(
            """\
            Run Date,Action,Symbol,Description,Type,Quantity,Price ($),Commission ($),Fees ($),Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date
            07/10/2025,"YOU BOUGHT TEST (Cash)",TST,"Test security",Cash,1,10,,,,-10,Processing,07/10/2025
            07/11/2025,"YOU SOLD TEST (Cash)",TST,"Test security",Cash,-1,12,,,,12,100.00,07/11/2025
            """
        )
    )

    parser = plugin.get_parser(str(csv_path))
    statement = parser.parse()

    assert len(statement.invest_lines) == 1
    assert statement.invest_lines[0].memo == "YOU SOLD TEST (Cash)"
    assert statement.end_balance == Decimal("100.00")


def test_parses_two_digit_year(tmp_path) -> None:
    plugin = FidelityPlugin(UI(), {})
    csv_path = tmp_path / "History_for_Account_456.csv"
    csv_path.write_text(
        dedent(
            """\
            Run Date,Action,Symbol,Description,Type,Quantity,Price ($),Commission ($),Fees ($),Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date
            07/11/25,"YOU SOLD TEST (Cash)",TST,"Test security",Cash,-1,12,,,,12,50.00,07/12/25
            """
        )
    )

    parser = plugin.get_parser(str(csv_path))
    statement = parser.parse()

    assert len(statement.invest_lines) == 1
    assert statement.invest_lines[0].date == datetime(2025, 7, 11)
    assert statement.invest_lines[0].date_user == datetime(2025, 7, 12)


def test_debit_card_purchase_action(tmp_path) -> None:
    plugin = FidelityPlugin(UI(), {})
    csv_path = tmp_path / "History_for_Account_789.csv"
    csv_path.write_text(
        dedent(
            """\
            Run Date,Action,Symbol,Description,Type,Price ($),Quantity,Commission ($),Fees ($),Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date
            12/22/25,DEBIT CARD PURCHASE PAYPAL *Li Yangyi Visa Direct CA1220252424818B20009G (Cash),,No Description,Cash,,0,,,,-200,0,
            12/22/25,DIRECT DEPOSIT PAYPAL TRANSFER (Cash), ,No Description,Cash,,0,,,,200,200,
            """
        )
    )

    parser = plugin.get_parser(str(csv_path))
    statement = parser.parse()

    assert len(statement.invest_lines) == 2
    details = {line.trntype_detailed for line in statement.invest_lines}
    assert details == {"DEBIT", "CREDIT"}


def test_redemption_from_core_account(tmp_path) -> None:
    plugin = FidelityPlugin(UI(), {})
    csv_path = tmp_path / "History_for_Account_321.csv"
    csv_path.write_text(
        dedent(
            """\
            Run Date,Action,Symbol,Description,Type,Price ($),Quantity,Commission ($),Fees ($),Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date
            12/19/25,REDEMPTION FROM CORE ACCOUNT FIDELITY GOVERNMENT MONEY MARKET (SPAXX) (Cash),SPAXX,FIDELITY GOVERNMENT MONEY MARKET,Cash,1,-426.42,,,,426.42,426.42,
            """
        )
    )

    parser = plugin.get_parser(str(csv_path))
    statement = parser.parse()

    assert len(statement.invest_lines) == 1
    assert statement.invest_lines[0].trntype_detailed == "CREDIT"
    assert statement.invest_lines[0].amount == Decimal("426.42")
