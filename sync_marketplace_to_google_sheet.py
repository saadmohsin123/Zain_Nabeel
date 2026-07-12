#!/usr/bin/env python3
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SPREADSHEET_ID = os.getenv('GOOGLE_SPREADSHEET_ID', '13u__qGNeV46Q9rREPbbDnzhZdNeNvxID4FGaH7Y47xo')
TOKEN_JSON = os.getenv('GOOGLE_TOKEN_JSON', '/Users/saadmohsin/Downloads/Master_sheet_automation/token.json')
INPUT_JSON = Path(os.getenv('MARKETPLACE_DRAFTS_JSON', 'marketplace_drafts.json'))
SECTION_ORDER = ['Pending Seller Action', 'Needs Review', 'Posted', 'Archived']
IMAGE_COLUMNS = [f'Image{i}' for i in range(1, 21)]
HEADERS = [
    'ListingKey',
    'ListingLifecycleStatus',
    'Address',
    'MarketplacePriceDisplay',
    'MarketplaceTitle',
    'MarketplaceDescription',
    'PrimaryImageURL',
    'ImageCount',
    'ImageURLs',
    *IMAGE_COLUMNS,
    'TransactionType',
    'PropertyType',
    'UnitNumber',
    'BedroomsTotal',
    'BathroomsTotal',
    'LivingAreaRange',
    'LegalStories',
    'PetsAllowed',
    'Basement',
    'HeatingDetails',
    'CoolingDetails',
    'GarageDetails',
    'LaundryDetails',
    'Amenities',
    'City',
    'MarketplaceStatus',
    'MarketplaceDocStatus',
    'MarketplacePostedAt',
    'MarketplaceBatchId',
    'MarketplaceDocIncludedAt',
    'GenerationMode',
    'GeneratedAt',
]
REBUILD_TABS = ['Overview', 'Sections'] + SECTION_ORDER
HEADER_BG = {'red': 0.85, 'green': 0.94, 'blue': 0.83}
HEADER_TEXT = {'foregroundColor': {'red': 0.15, 'green': 0.15, 'blue': 0.15}}


def col_index(name: str) -> int:
    return HEADERS.index(name)


def load_creds() -> Credentials:
    creds = Credentials.from_authorized_user_file(TOKEN_JSON)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_JSON, 'w', encoding='utf-8') as f:
            f.write(creds.to_json())
    return creds


def sheets_service():
    creds = load_creds()
    return build('sheets', 'v4', credentials=creds)


def load_drafts() -> List[Dict]:
    drafts = json.loads(INPUT_JSON.read_text(encoding='utf-8'))
    normalized = []
    for d in drafts:
        row = {h: d.get(h, '') for h in HEADERS}
        row['ListingLifecycleStatus'] = 'Active'
        row['MarketplaceStatus'] = 'Pending Seller Action'
        row['MarketplaceDocStatus'] = 'Pending Seller Action'
        normalized.append(row)
    return normalized


def get_metadata(service):
    return service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()


def ensure_tabs(service, metadata):
    existing = {s['properties']['title']: s['properties']['sheetId'] for s in metadata['sheets']}
    requests = []
    if 'Sheet1' in existing and len(existing) == 1:
        requests.append({'updateSheetProperties': {'properties': {'sheetId': existing['Sheet1'], 'title': 'Overview', 'index': 0}, 'fields': 'title,index'}})
        existing['Overview'] = existing.pop('Sheet1')
    for idx, name in enumerate(REBUILD_TABS):
        if name not in existing:
            requests.append({'addSheet': {'properties': {'title': name, 'index': idx}}})
    if requests:
        service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={'requests': requests}).execute()
        metadata = get_metadata(service)
    return {s['properties']['title']: s['properties']['sheetId'] for s in metadata['sheets']}


def apply_overview_validations(service, sheet_id: int):
    status_col = col_index('MarketplaceStatus')
    requests = [
        {
            'setDataValidation': {
                'range': {
                    'sheetId': sheet_id,
                    'startRowIndex': 1,
                    'endRowIndex': 5000,
                    'startColumnIndex': 0,
                    'endColumnIndex': len(HEADERS),
                },
                'rule': None,
            }
        },
        {
            'updateSheetProperties': {
                'properties': {
                    'sheetId': sheet_id,
                    'gridProperties': {'frozenRowCount': 1},
                },
                'fields': 'gridProperties.frozenRowCount',
            }
        },
        {
            'setBasicFilter': {
                'filter': {
                    'range': {
                        'sheetId': sheet_id,
                        'startRowIndex': 0,
                        'startColumnIndex': 0,
                        'endColumnIndex': len(HEADERS),
                    }
                }
            }
        },
        {
            'setDataValidation': {
                    'range': {
                        'sheetId': sheet_id,
                        'startRowIndex': 1,
                        'startColumnIndex': status_col,
                        'endColumnIndex': status_col + 1,
                    },
                    'rule': {
                        'condition': {
                            'type': 'ONE_OF_LIST',
                            'values': [{'userEnteredValue': v} for v in SECTION_ORDER],
                    },
                    'strict': True,
                    'showCustomUi': True,
                },
            }
        },
    ]

    # Replace existing conditional formatting rules on the overview sheet
    try:
        spreadsheet = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
        overview = next((s for s in spreadsheet['sheets'] if s['properties']['sheetId'] == sheet_id), None)
        existing_rules = len(overview.get('conditionalFormats', [])) if overview else 0
    except Exception:
        existing_rules = 0

    for idx in reversed(range(existing_rules)):
        requests.append({'deleteConditionalFormatRule': {'sheetId': sheet_id, 'index': idx}})

    color_rules = [
        ('Posted', {'red': 0.85, 'green': 0.94, 'blue': 0.83}),
        ('Needs Review', {'red': 1.0, 'green': 0.95, 'blue': 0.8}),
        ('Archived', {'red': 0.9, 'green': 0.9, 'blue': 0.9}),
        ('Pending Seller Action', {'red': 0.84, 'green': 0.91, 'blue': 0.98}),
    ]
    for idx, (label, color) in enumerate(color_rules):
        requests.append(
            {
                'addConditionalFormatRule': {
                    'rule': {
                        'ranges': [
                            {
                                'sheetId': sheet_id,
                                'startRowIndex': 1,
                                'endRowIndex': 5000,
                                'startColumnIndex': 0,
                                'endColumnIndex': len(HEADERS),
                            }
                        ],
                        'booleanRule': {
                            'condition': {
                                'type': 'CUSTOM_FORMULA',
                                'values': [
                                    {
                                        'userEnteredValue': f'=${chr(65 + status_col)}2="{label}"'
                                    }
                                ],
                            },
                            'format': {
                                'backgroundColor': color,
                            },
                        },
                    },
                    'index': idx,
                }
            }
        )

    service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={'requests': requests}).execute()


def get_existing_overview_rows(service) -> List[Dict]:
    try:
        resp = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range='Overview!A1:AE5000').execute()
    except HttpError:
        return []
    values = resp.get('values', [])
    if not values:
        return []
    headers = values[0]
    rows = []
    for row in values[1:]:
        padded = row + [''] * (len(headers) - len(row))
        rec = dict(zip(headers, padded))
        if rec.get('ListingKey'):
            rows.append(rec)
    return rows


def merge_rows(current_rows: List[Dict], existing_rows: List[Dict]) -> List[Dict]:
    current_by_key = {r['ListingKey']: r for r in current_rows if r.get('ListingKey')}
    merged = []
    seen = set()
    for listing_key, row in current_by_key.items():
        prev = next((r for r in existing_rows if r.get('ListingKey') == listing_key), None)
        if prev:
            for field in ['MarketplacePostedAt', 'MarketplaceBatchId', 'MarketplaceDocIncludedAt', 'MarketplaceStatus', 'MarketplaceDocStatus']:
                if prev.get(field) not in ('', None) and row.get(field) in ('', None, False):
                    row[field] = prev.get(field)
            if prev.get('MarketplaceStatus') in {'Pending Seller Action'}:
                row['MarketplaceStatus'] = prev.get('MarketplaceStatus')
                if prev.get('MarketplaceDocIncludedAt'):
                    row['MarketplaceDocIncludedAt'] = prev.get('MarketplaceDocIncludedAt')
            elif prev.get('MarketplaceStatus') in {'Needs Review', 'Archived'}:
                row['MarketplaceStatus'] = prev.get('MarketplaceStatus')
                if prev.get('MarketplaceDocIncludedAt'):
                    row['MarketplaceDocIncludedAt'] = prev.get('MarketplaceDocIncludedAt')
            row['MarketplaceDocStatus'] = row['MarketplaceStatus']
        merged.append(row)
        seen.add(listing_key)
    for prev in existing_rows:
        key = prev.get('ListingKey')
        if not key or key in seen:
            continue
        expired = {h: prev.get(h, '') for h in HEADERS}
        expired['ListingLifecycleStatus'] = 'Expired'
        if expired.get('MarketplaceStatus') not in {'Posted', 'Archived'}:
            expired['MarketplaceStatus'] = 'Archived'
        expired['MarketplaceDocStatus'] = expired['MarketplaceStatus']
        merged.append(expired)
    merged.sort(key=lambda r: (r.get('ListingLifecycleStatus') != 'Active', -(float(r.get('MarketplacePrice') or 0) if str(r.get('MarketplacePrice') or '').replace('.','',1).isdigit() else 0)))
    return merged


def rows_for_section(overview_rows: List[Dict], section: str) -> List[Dict]:
    if section == 'Archived':
        return [r for r in overview_rows if r.get('MarketplaceStatus') == 'Archived' or r.get('ListingLifecycleStatus') == 'Expired']
    return [r for r in overview_rows if r.get('MarketplaceStatus') == section]


def values_from_rows(rows: List[Dict]):
    output = [HEADERS]
    for row in rows:
        vals = []
        for h in HEADERS:
            val = row.get(h, '')
            if isinstance(val, bool):
                vals.append(val)
            elif h in IMAGE_COLUMNS and val not in ('', None):
                # Store plain image URLs only (no =IMAGE() previews in the sheet).
                vals.append(str(val))
            else:
                vals.append('' if val is None else str(val))
        output.append(vals)
    return output


def update_tab(service, sheet_name: str, values: List[List[str]]):
    service.spreadsheets().values().clear(spreadsheetId=SPREADSHEET_ID, range=f"'{sheet_name}'").execute()
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{sheet_name}'!A1",
        valueInputOption='USER_ENTERED',
        body={'values': values},
    ).execute()


def apply_tab_theme(service, sheet_id: int, sheet_name: str, column_count: int, row_count: int):
    requests = []
    requests.append(
        {
            'updateSheetProperties': {
                'properties': {
                    'sheetId': sheet_id,
                    'gridProperties': {'frozenRowCount': 1},
                },
                'fields': 'gridProperties.frozenRowCount',
            }
        }
    )
    requests.append(
        {
            'repeatCell': {
                'range': {
                    'sheetId': sheet_id,
                    'startRowIndex': 0,
                    'endRowIndex': 1,
                    'startColumnIndex': 0,
                    'endColumnIndex': column_count,
                },
                'cell': {
                    'userEnteredFormat': {
                        'backgroundColor': HEADER_BG,
                        'textFormat': {
                            'bold': True,
                            **HEADER_TEXT,
                        },
                    }
                },
                'fields': 'userEnteredFormat(backgroundColor,textFormat.bold,textFormat.foregroundColor)',
            }
        }
    )
    requests.append(
        {
            'setBasicFilter': {
                'filter': {
                    'range': {
                        'sheetId': sheet_id,
                        'startRowIndex': 0,
                        'startColumnIndex': 0,
                        'endColumnIndex': column_count,
                    }
                }
            }
        }
    )
    requests.append(
        {
            'updateDimensionProperties': {
                'range': {
                    'sheetId': sheet_id,
                    'dimension': 'COLUMNS',
                    'startIndex': 0,
                    'endIndex': column_count,
                },
                'properties': {
                    'pixelSize': 180,
                },
                'fields': 'pixelSize',
            }
        }
    )
    requests.append(
        {
            'autoResizeDimensions': {
                'dimensions': {
                    'sheetId': sheet_id,
                    'dimension': 'COLUMNS',
                    'startIndex': 0,
                    'endIndex': column_count,
                }
            }
        }
    )
    requests.append(
        {
            'repeatCell': {
                'range': {
                    'sheetId': sheet_id,
                    'startRowIndex': 1,
                    'endRowIndex': max(row_count, 2),
                    'startColumnIndex': 0,
                    'endColumnIndex': column_count,
                },
                'cell': {
                    'userEnteredFormat': {
                        'verticalAlignment': 'TOP',
                        'wrapStrategy': 'WRAP',
                    }
                },
                'fields': 'userEnteredFormat(verticalAlignment,wrapStrategy)',
            }
        }
    )
    try:
        existing = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
        sheet = next((s for s in existing['sheets'] if s['properties']['sheetId'] == sheet_id), None)
        if sheet and sheet.get('filterViews'):
            for idx in reversed(range(len(sheet.get('filterViews', [])))):
                requests.append({'deleteFilterView': {'filterId': sheet['filterViews'][idx]['filterViewId']}})
    except Exception:
        pass
    service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={'requests': requests}).execute()


def main():
    service = sheets_service()
    metadata = get_metadata(service)
    tab_ids = ensure_tabs(service, metadata)

    current_rows = load_drafts()
    existing_rows = get_existing_overview_rows(service)
    overview_rows = merge_rows(current_rows, existing_rows)

    update_tab(service, 'Overview', values_from_rows(overview_rows))
    apply_tab_theme(service, tab_ids['Overview'], 'Overview', len(HEADERS), len(overview_rows) + 1)
    sections = [['Section', 'Count']]
    for sec in SECTION_ORDER:
        sec_rows = rows_for_section(overview_rows, sec)
        sections.append([sec, str(len(sec_rows))])
        update_tab(service, sec, values_from_rows(sec_rows))
        apply_tab_theme(service, tab_ids[sec], sec, len(HEADERS), len(sec_rows) + 1)
    update_tab(service, 'Sections', sections)
    apply_tab_theme(service, tab_ids['Sections'], 'Sections', 2, len(sections))
    apply_overview_validations(service, tab_ids['Overview'])

    print(f'Spreadsheet updated: {SPREADSHEET_ID}')
    print(f'Overview rows: {len(overview_rows)}')
    for sec in SECTION_ORDER:
        print(f'{sec}: {len(rows_for_section(overview_rows, sec))}')


if __name__ == '__main__':
    main()
