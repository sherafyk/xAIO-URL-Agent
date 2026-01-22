from __future__ import annotations

import logging
from typing import Any, Dict, List

from gspread import Worksheet
from gspread.exceptions import APIError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((APIError,)),
)
def batch_update_row_cells(
    wks: Worksheet,
    row: int,
    col_to_value: Dict[str, Any],
    *,
    value_input_option: str = "RAW",
) -> None:
    """
    Update multiple single-cell ranges in one Sheets API call.

    col_to_value keys are column letters like "B", "F", "K".
    """
    if not col_to_value:
        return

    data: List[dict] = []
    for col, value in col_to_value.items():
        a1 = f"{col}{row}"
        data.append({"range": a1, "values": [[value]]})

    wks.batch_update(data, value_input_option=value_input_option)
