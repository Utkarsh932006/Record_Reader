from __future__ import annotations

from pathlib import Path

import pandas as pd


def write_report(sheets: list[tuple[str, pd.DataFrame, list[str], list[str]]], output_path: Path, table_style: str) -> None:
    """Write report sheets to Excel.

    sheets: list of (sheet_name, dataframe, text_cols, pct_cols).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path, engine="xlsxwriter") as writer:
        workbook = writer.book
        pct_fmt = workbook.add_format({"num_format": "0.00%"})
        text_fmt = workbook.add_format({"num_format": "@"})

        for sheet_name, df, _txt_cols, _pct_cols in sheets:
            df.to_excel(writer, index=False, sheet_name=sheet_name)

        for sheet_name, df, txt_cols, pct_cols in sheets:
            if df.empty:
                continue
            worksheet = writer.sheets[sheet_name]
            worksheet.add_table(
                0,
                0,
                max(1, df.shape[0]),
                max(0, df.shape[1] - 1),
                {
                    "columns": [{"header": c} for c in df.columns],
                    "style": table_style,
                },
            )
            for i, col_name in enumerate(df.columns):
                max_val_len = (
                    df[col_name].map(lambda x: len(str(x))).max() if not df.empty else 0
                )
                max_len = max(len(str(col_name)), int(max_val_len) if pd.notna(max_val_len) else 0)
                fmt = None
                if col_name in txt_cols:
                    fmt = text_fmt
                elif col_name in pct_cols:
                    fmt = pct_fmt
                worksheet.set_column(i, i, max(max_len + 2, 12), fmt)
