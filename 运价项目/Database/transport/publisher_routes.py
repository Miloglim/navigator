"""Publisher HTTP routing; storage and inference are supplied by adapters."""
from core.contracts import PUBLISHER_ACCOUNTS_ROUTE


def build_publisher_routes(read_accounts, write_decision):
    return ({PUBLISHER_ACCOUNTS_ROUTE: lambda query: read_accounts()},
            {PUBLISHER_ACCOUNTS_ROUTE: write_decision})
