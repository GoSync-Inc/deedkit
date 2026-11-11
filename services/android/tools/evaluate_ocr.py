#!/usr/bin/env python3
"""Measure text recognition on a labelled screen set and print numbers only.

Usage (on the USB host, from services/android or the installed release's venv):
  python tools/evaluate_ocr.py SET_DIR --command /opt/homebrew/bin/tesseract [--languages eng+ind+rus]
  python tools/evaluate_ocr.py SET_DIR --command /opt/homebrew/bin/tesseract --engine vision
  python tools/evaluate_ocr.py SET_DIR --tsv-command '/path/vision_tsv {image} {languages}'

SET_DIR holds <name>.png and <name>.json ground truth (deedkit_android.ocr_quality). The private set
lives only in the host's private state directory: never in Git, images or logs. Output is one JSON
object of counts, percentages, seconds and screen names; no recognised or expected text is printed.
--engine vision measures Apple Vision as readScreenText runs it (macOS with the vision extra; a Vision
failure falls back to --command). --tsv-command runs another local engine that prints Tesseract TSV
word rows for the bake-off; it gets the raw screen, without the Tesseract preprocessing.
"""

import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

from deedkit_android.config import Refused
from deedkit_android.ocr_quality import evaluate
from deedkit_android.screen_text import engine_lines, recognize


def tsv_engine(template):
    def run(image, ocr):
        with tempfile.TemporaryDirectory(prefix='deedkit-ocr-eval-') as directory:
            path = Path(directory) / 'screen.png'
            image.save(path, format='PNG', compress_level=1)
            argv = [part.format(image=path, languages=ocr['languages']) for part in shlex.split(template)]
            done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=120, check=False)
        if done.returncode:
            raise Refused('ocr_failed')
        return engine_lines(done.stdout.decode('utf-8', 'replace'), (0, 0), 60)
    return run


def main(argv=None):
    parser = argparse.ArgumentParser(description='Measure text recognition on a labelled screen set.')
    parser.add_argument('set_dir')
    engine = parser.add_mutually_exclusive_group(required=True)
    engine.add_argument('--command', help='absolute Tesseract path (the service engine and preprocessing)')
    engine.add_argument('--tsv-command', help='another local engine; {image} and {languages} are filled in')
    parser.add_argument('--languages', help='override every screen\'s languages, for example eng+ind')
    parser.add_argument('--engine', choices=('tesseract', 'vision'), default='tesseract',
                        help='with --command: the readScreenText engine to measure')
    args = parser.parse_args(argv)
    if args.tsv_command:
        summary = evaluate(args.set_dir, None, args.languages, tsv_engine(args.tsv_command))
    else:
        engine = args.engine
        summary = evaluate(args.set_dir, args.command, args.languages,
                           lambda image, ocr: recognize(image, {**ocr, 'engine': engine}))
    json.dump(summary, sys.stdout)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
