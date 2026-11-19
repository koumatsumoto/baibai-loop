"""任意の要素/context/valueから作る合成EDINET CSV原ZIP。"""

import zipfile
from io import BytesIO


def _edinet_csv_zip(rows: list[tuple[str, str, str]]) -> bytes:
    csv_text = "要素ID\tコンテキストID\tユニットID\t値\n" + "\n".join(
        f"{element}\t{context}\tJPY\t{value}" for element, context, value in rows
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("XBRL_TO_CSV/sample.csv", csv_text.encode("utf-16"))
    return buffer.getvalue()


def damaged_archive_directory() -> bytes:
    """The first ZIP directory entry is intact, but the next entry cannot be opened."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", b"")
        archive.writestr("XBRL_TO_CSV/filing.csv", "original".encode("utf-16"))
    before, marker, after = buffer.getvalue().rpartition(b"PK\x01\x02")
    assert marker
    assert marker in before
    return before + b"lost" + after
