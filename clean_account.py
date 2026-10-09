from pathlib import Path
import sys

import pandas as pd


def clean_accounts(data):
    """Return cleaned account data, rejecting invalid amounts and dates."""
    cleaned = data.copy()
    text_columns = ['accountId', 'status', 'accountType', 'currency']
    required = text_columns + ['balance', 'dailyLimit', 'openedDate']
    missing = [column for column in required if column not in cleaned.columns]
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")

    # Both datasets have eight standard columns: remove rows missing four or more.
    fields = required + ['customerName'] if 'customerName' in cleaned else required
    blank = cleaned[fields].apply(lambda values: values.fillna('').str.strip().eq(''))
    cleaned = cleaned.loc[blank.sum(axis=1) < 4].copy()

    for column in text_columns:
        cleaned[column] = cleaned[column].str.strip().str.upper()

    for column in ['balance', 'dailyLimit', 'openedDate']:
        values = cleaned[column].str.strip().replace('', pd.NA)
        try:
            if column == 'openedDate':
                cleaned[column] = pd.to_datetime(
                    values, format='mixed', dayfirst=False, errors='raise'
                ).dt.strftime('%Y-%m-%d')
            else:
                numbers = pd.to_numeric(values, errors='raise')
                if numbers.isin([float('inf'), float('-inf')]).any():
                    raise ValueError('infinite amounts are not valid')
                cleaned[column] = numbers.astype(float)
        except (ValueError, TypeError, OverflowError) as error:
            raise ValueError(f"Invalid value in '{column}': {error}") from error

    return cleaned


def main():
    folder = Path(__file__).resolve().parent
    source = folder / 'accounts.csv'
    output = folder / 'accounts_cleaned.csv'
    # Read as text to preserve identifiers, empty cells, and untouched values.
    data = pd.read_csv(source, dtype=str, keep_default_na=False)
    cleaned = clean_accounts(data)
    print(f'Removed {len(data) - len(cleaned)} rows missing at least four fields.')
    cleaned.to_csv(output, index=False, float_format='%.2f', na_rep='')
    print(f'Cleaned accounts saved to: {output}')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        sys.exit(f'Error: {error}')

