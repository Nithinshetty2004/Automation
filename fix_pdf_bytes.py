
import sys
from pathlib import Path

def recover_cp1252_utf8(data: bytes) -> bytes:
    # The damaged file must contain valid UTF-8 text produced by
    # encoding characters that originally represented PDF bytes.
    text = data.decode("utf-8", errors="strict")

    recovered = bytearray()

    # Windows-1252 leaves these byte values undefined. Preserve their
    # corresponding control-code values if they occur in the text.
    undefined_cp1252 = {
        "\u0081": 0x81,
        "\u008d": 0x8D,
        "\u008f": 0x8F,
        "\u0090": 0x90,
        "\u009d": 0x9D,
    }

    for pos, char in enumerate(text):
        if char in undefined_cp1252:
            recovered.append(undefined_cp1252[char])
            continue

        try:
            recovered.extend(char.encode("cp1252"))
        except UnicodeEncodeError as exc:
            raise ValueError(
                f"Cannot reverse character U+{ord(char):04X} "
                f"at character position {pos}: {char!r}. "
                "No output PDF was written."
            ) from exc

    return bytes(recovered)


def main():
    if len(sys.argv) != 2:
        print('Usage: python fix_pdf_bytes.py "path\\to\\file.pdf"')
        sys.exit(1)

    source = Path(sys.argv[1])

    if not source.is_file():
        raise FileNotFoundError(f"File not found: {source}")

    original = source.read_bytes()
    repaired = recover_cp1252_utf8(original)

    if not repaired.startswith(b"%PDF-"):
        raise ValueError(
            "Recovered bytes do not start with %PDF-. "
            "The assumed encoding may be incorrect."
        )

    if b"%%EOF" not in repaired[-2048:]:
        raise ValueError(
            "PDF EOF marker not found near the end. "
            "The file may still be damaged."
        )

    output = source.with_name(source.stem + "_recovered.pdf")
    output.write_bytes(repaired)

    print(f"Original size: {len(original):,} bytes")
    print(f"Recovered size: {len(repaired):,} bytes")
    print(f"Saved to: {output}")
    print("Basic PDF signature checks passed.")
    print("Open the recovered file and verify its pages.")


if __name__ == "__main__":
    main()
