"""Normalize transactions without inventing missing financial data or dropping rows."""

from pathlib import Path
import sys

import pandas as pd


def clean_transactions(data):
    required = ['transactionId', 'timestamp', 'type', 'fromAccount',
                'toAccount', 'amount', 'channel', 'description']
    missing = [column for column in required if column not in data.columns]
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")
    cleaned = data.copy().reset_index(drop=True)
    for column in required:
        cleaned[column] = cleaned[column].fillna('').astype(str).str.strip()
    for column in ['transactionId', 'type', 'fromAccount', 'toAccount', 'channel']:
        cleaned[column] = cleaned[column].str.upper()

    issues = []

    def flag(mask, message):
        for index in cleaned.index[mask]:
            issues.append({'sourceRow': index + 2,
                           'transactionId': cleaned.at[index, 'transactionId'],
                           'issue': message})

    for column in ['transactionId', 'timestamp', 'type', 'amount']:
        flag(cleaned[column].eq(''), f'Missing {column}; left blank')
    for column in ['channel', 'description']:
        flag(cleaned[column].eq(''), f'Missing {column}; left blank')

    amounts = pd.to_numeric(cleaned['amount'], errors='coerce').astype(float)
    invalid_amount = amounts.isna() | amounts.isin([float('inf'), float('-inf')])
    flag(cleaned['amount'].ne('') & invalid_amount, 'Invalid amount; original value retained')
    flag(~invalid_amount & amounts.le(0), 'Nonpositive amount; retained for review')
    cleaned.loc[~invalid_amount, 'amount'] = amounts[~invalid_amount].map(lambda value: f'{value:.2f}')

    # Parse each timestamp to support mixed formats without discarding time or offset.
    for index, value in cleaned['timestamp'].items():
        if not value:
            continue
        try:
            timestamp = pd.to_datetime(value, dayfirst=False, errors='raise')
            if pd.isna(timestamp):
                raise ValueError('Not a timestamp')
            cleaned.at[index, 'timestamp'] = timestamp.isoformat()
        except (ValueError, TypeError, OverflowError):
            flag(cleaned.index == index, 'Invalid timestamp; original value retained')

    kinds = cleaned['type']
    flag(kinds.isin(['TRANSFER', 'PURCHASE', 'WITHDRAWAL']) & cleaned['fromAccount'].eq(''),
         'Missing required fromAccount; left blank')
    flag(kinds.isin(['TRANSFER', 'DEPOSIT']) & cleaned['toAccount'].eq(''),
         'Missing required toAccount; left blank')
    flag(kinds.eq('REVERSAL') & cleaned['fromAccount'].eq('') & cleaned['toAccount'].eq(''),
         'Reversal has no account; left blank')
    flag(kinds.ne('') & ~kinds.isin(['TRANSFER', 'PURCHASE', 'WITHDRAWAL', 'DEPOSIT', 'REVERSAL']),
         'Unrecognized transaction type; retained for review')
    flag(cleaned['transactionId'].ne('') & cleaned['transactionId'].duplicated(keep=False),
         'Repeated transactionId; all occurrences retained for review')
    report = pd.DataFrame(issues, columns=['sourceRow', 'transactionId', 'issue'])
    return cleaned, report


def main():
    folder = Path(__file__).resolve().parent
    data = pd.read_csv(folder / 'transactions.csv', dtype=str, keep_default_na=False)
    cleaned, issues = clean_transactions(data)
    output = folder / 'transactions_cleaned.csv'
    cleaned.to_csv(output, index=False)
    print(f'Saved {len(cleaned)} transactions to: {output}')
    if not issues.empty:
        print(f'Found {len(issues)} review issues:')
        print(issues.to_string(index=False))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        sys.exit(f'Error: {error}')
