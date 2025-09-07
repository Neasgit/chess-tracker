import io
import os
import csv
import requests
import zstandard as zstd
from urllib.parse import urlparse, unquote
from sqlalchemy import text

UPSERT = text("""
INSERT INTO puzzles (puzzle_id, rating, rating_deviation, popularity, nb_plays, themes, game_url, fen, moves)
VALUES (:id,:rating,:rd,:pop,:nb,:themes,:url,:fen,:moves)
ON CONFLICT(puzzle_id) DO UPDATE SET
  rating=excluded.rating,
  rating_deviation=excluded.rating_deviation,
  popularity=excluded.popularity,
  nb_plays=excluded.nb_plays,
  themes=excluded.themes,
  game_url=excluded.game_url,
  fen=excluded.fen,
  moves=excluded.moves
""")

def _open_stream(url: str):
    parsed = urlparse(url)
    dctx = zstd.ZstdDecompressor()
    if parsed.scheme == "file":
        local_path = unquote(parsed.path)
        f = open(local_path, "rb")
        return io.TextIOWrapper(dctx.stream_reader(f), encoding="utf-8", newline="")
    elif parsed.scheme in ("http", "https"):
        r = requests.get(url, stream=True, timeout=600)
        r.raise_for_status()
        return io.TextIOWrapper(dctx.stream_reader(r.raw), encoding="utf-8", newline="")
    raise ValueError(f"Unsupported URL scheme: {url}")

def _get(row, *names, default=None):
    for n in names:
        if n in row and row[n] != "":
            return row[n]
    return default

def run(url: str, engine):
    reader = _open_stream(url)
    dr = csv.DictReader(reader)

    fieldnames = dr.fieldnames or []
    print(f"[sync_puzzles] CSV columns: {fieldnames[:10]}{'...' if len(fieldnames)>10 else ''}")

    batch, BATCH = [], 5000
    total = 0
    skipped = 0

    def flush():
        nonlocal batch, total
        if not batch:
            return
        with engine.begin() as conn:
            conn.execute(UPSERT, batch)
        total += len(batch)
        batch.clear()
        print(f"[sync_puzzles] Upserted {total:,} rows...", end="\r")

    for row in dr:
        try:
            pid = _get(row, "id", "PuzzleId")            # TEXT id
            rating = _get(row, "rating", "Rating")
            rd = _get(row, "rd", "RatingDeviation", default=0)
            pop = _get(row, "popularity", "Popularity", default=0)
            nbp = _get(row, "nbPlays", "NbPlays", default=0)
            themes = _get(row, "themes", "Themes", default="") or ""
            url_ = _get(row, "gameUrl", "GameUrl", default="") or ""
            fen = _get(row, "FEN", default=None)
            moves = _get(row, "moves", "Moves", default=None)

            rec = {
                "id": str(pid),
                "rating": int(rating),
                "rd": int(rd or 0),
                "pop": int(pop or 0),
                "nb": int(nbp or 0),
                "themes": themes.strip(),
                "url": url_.strip(),
                "fen": fen.strip(),
                "moves": moves.strip(),
            }
        except Exception:
            skipped += 1
            continue

        batch.append(rec)
        if len(batch) >= BATCH:
            flush()

    flush()
    print(f"\n[sync_puzzles] Done. Total upserted: {total:,}. Skipped: {skipped:,}.")
