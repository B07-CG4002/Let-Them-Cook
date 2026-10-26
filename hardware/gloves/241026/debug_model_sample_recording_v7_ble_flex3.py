"""Launch the independent-hand three-flex BLE recorder."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from src.hand_ai_flex3.record_gui import main
if __name__ == '__main__':
    main()
