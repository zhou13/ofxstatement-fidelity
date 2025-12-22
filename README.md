# ofxstatement-fidelity

Parse Fidelity CSV exports into OFX using the `ofxstatement` plugin system. Works with standard History/Accounts_History exports and auto-detects column order from the CSV header.

## Install

Install straight from GitHub (pulls `ofxstatement` as a dependency):

```bash
pip install git+https://github.com/zhou13/ofxstatement-fidelity
```

## Usage

Export your transactions from Fidelity as CSV, then convert:

```bash
ofxstatement convert -t fidelity <path-to-History_for_Account_XXXXX.csv> output.ofx
```

The plugin reads the header row to map columns, handles common cash, buy/sell, dividend, interest, transfer, and fee/tax actions, and sets account_id from the filename or embedded account number when present.

## Development (uv)

Use [uv](https://docs.astral.sh/uv/) for a fast, isolated dev environment.

```bash
uv sync --extra dev
```

Run checks and tests:

```bash
uv run pytest
```

To build a wheel/sdist locally:

```bash
uv run python -m build
```

## Notes

- Column mapping is derived from the header; odd or reordered exports should still parse.
- Splits and spin-offs lack ratio data in Fidelity exports; they arrive as transfer-style entries and may need manual cost-basis adjustments in your accounting software.
