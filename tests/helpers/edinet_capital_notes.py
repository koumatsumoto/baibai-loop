"""公開taxonomyの資本注記構造を使う小さな合成例。実企業の原本ではない。

https://www.fsa.go.jp/search/20251111.html の2026年版要素リストと
サンプルS002XXXXの表構造を参照し、値とXMLはこのtest用に作成している。
"""

import io
import zipfile
from html import escape

CRP = "http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2025-11-01/jpcrp_cor"
PFS = "http://disclosure.edinet-fsa.go.jp/taxonomy/jppfs/2025-11-01/jppfs_cor"
BLOCK = "NotesRegardingIssuedSharesAndTreasurySharesTextBlock"


def table(*, issued="100,000", treasury="500", unit="株", label="当連結会計年度"):
    rows = [
        [
            "",
            f"{label}期首株式数（{unit}）",
            f"{label}増加株式数（{unit}）",
            f"{label}減少株式数（{unit}）",
            f"{label}末株式数（{unit}）",
        ],
        ["発行済株式", "", "", "", ""],
        ["普通株式", "80,000", "20,000", "－", issued],
        ["合計", "80,000", "20,000", "－", issued],
        ["自己株式", "", "", "", ""],
        ["普通株式（注）１,２", "100", "400", "－", treasury],
        ["合計", "100", "400", "－", treasury],
    ]
    return (
        "<table><tbody>"
        + "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
        + "</tbody></table>"
    )


def context(
    *,
    key="CurrentYearDuration",
    start="2025-04-01",
    end="2026-03-31",
    issuer="E12345-000",
    dimensions="",
):
    period = (
        f"<x:startDate>{start}</x:startDate><x:endDate>{end}</x:endDate>"
        if start
        else f"<x:instant>{end}</x:instant>"
    )
    return (
        f'<x:context id="{key}"><x:entity><x:identifier '
        f'scheme="http://disclosure.edinet-fsa.go.jp">{issuer}</x:identifier></x:entity>'
        f"<x:period>{period}</x:period>{dimensions}</x:context>"
    )


def block(html=None, *, key="CurrentYearDuration", attributes=""):
    return f'<c:{BLOCK} contextRef="{key}" {attributes}>{escape(html if html is not None else table())}</c:{BLOCK}>'


def source_zip(*, contexts=None, blocks=None, other="", namespace=CRP):
    content = (
        f'<x:xbrl xmlns:x="http://www.xbrl.org/2003/instance" xmlns:c="{namespace}" '
        f'xmlns:p="{PFS}" xmlns:d="http://xbrl.org/2006/xbrldi" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        + (context() if contexts is None else contexts)
        + (block() if blocks is None else blocks)
        + other
        + "</x:xbrl>"
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("XBRL/PublicDoc/issuer.xbrl", content)
    return output.getvalue()
