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


def damaged_deflated_csv() -> bytes:
    """A damaged compressed member; its directory, name and CRC field remain intact."""
    archive_path = "XBRL_TO_CSV/result.csv"
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(archive_path, "要素ID\t値\n結果\t買付けを行います".encode("utf-16"))
    content = bytearray(buffer.getvalue())
    start = content.index(archive_path.encode("ascii")) + len(archive_path)
    content[start] |= 0b110  # unreadable first DEFLATE block, not a stored-member CRC mismatch
    return bytes(content)
