"""OnePlus 15 main entry: Stock, Stock+schedutil, Pavo+schedutil."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_main import main

if __name__ == "__main__":
    raise SystemExit(main(["--platform", "oneplus15", *sys.argv[1:]]))
