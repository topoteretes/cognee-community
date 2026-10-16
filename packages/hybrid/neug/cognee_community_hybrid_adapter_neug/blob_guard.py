"""VARCHAR blob-size guard shared by the NeuG graph and vector adapters.

Both adapters store JSON blobs in NeuG ``VARCHAR(65535)`` columns: the graph
adapter's node/edge ``properties``, and the vector adapter's ``text``,
``payload`` and ``belongs_to_set``. NeuG **silently truncates** any value whose
UTF-8 encoding exceeds the column's byte capacity instead of raising. A
truncated JSON blob then fails to decode and ``_parse_properties_blob``
swallows the parse error into ``{}``, so an oversized write would silently drop
every property (and, for ``text``, desync the stored string from the embedding
that was computed on the full value). ``ensure_blob_fits`` turns that silent
corruption into an explicit error at the write boundary.

The limit is on UTF-8 **bytes**, not characters, so multi-byte text reaches it
sooner than its character count suggests.
"""

# Byte capacity of the adapters' ``VARCHAR(65535)`` blob columns.
MAX_VARCHAR_BYTES = 65535


def ensure_blob_fits(value: str, *, column: str, ident: str = "") -> str:
    """Return ``value`` unchanged, or raise if it would be silently truncated.

    Wraps the assignment inline (it returns ``value``) so the guard reads as
    part of the row/record construction. ``column`` names the VARCHAR column
    and ``ident`` the row key, both for the error message.
    """
    encoded = len(value.encode("utf-8"))
    if encoded > MAX_VARCHAR_BYTES:
        where = f" for id={ident!r}" if ident else ""
        raise ValueError(
            f"NeuG column '{column}'{where} cannot store {encoded} bytes: it "
            f"exceeds the VARCHAR({MAX_VARCHAR_BYTES}) byte capacity and would "
            "be silently truncated, corrupting the stored JSON/text. Reduce the "
            "value size (e.g. chunk the input) before writing."
        )
    return value
