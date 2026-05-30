import sys

from src.main import main, test_single_row


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        test_single_row()
    else:
        main()
