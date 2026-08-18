"""独立链路自检入口；默认只做 import 和参数/语法检查，不调用 API。
使用 --live CONFIG.json SONG 可实际跑完整链路并消耗 API 配额。
"""
import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _sing_core import execute_pipeline, best_lyric_start, validate_output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="实际调用火山引擎的 TTS、实时对话和 ASR")
    parser.add_argument("config", nargs="?")
    parser.add_argument("song", nargs="?")
    parser.add_argument("--lyrics", default="")
    args = parser.parse_args()
    root = Path(__file__).parent
    json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    assert best_lyric_start([{"start": 4.0, "text": "我想要"}], "我想要") == (4.0, 1.0)
    assert validate_output("小幸运", "我想要", {"text": "我想要"}, 8.0)[0]
    if not args.live:
        print("elysia_sing selftest: imports and pure logic OK; no network calls made")
        return
    if not args.config or not args.song:
        parser.error("--live 需要 CONFIG.json 和 SONG")
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="elysia_sing_selftest_") as directory:
        final_path, details = asyncio.run(execute_pipeline(
            config, directory, args.song, f"请唱{args.song}", args.lyrics
        ))
        print(json.dumps({"final_wav": str(final_path), "details": details}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
