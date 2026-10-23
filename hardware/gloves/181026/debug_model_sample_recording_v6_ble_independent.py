"""Launch the independent-hand BLE GUI from the familiar recorder folder."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from src.hand_ai.record_gui import main

if __name__ == '__main__':
    main()
