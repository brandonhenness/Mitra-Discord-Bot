import os

os.environ.setdefault("MITRA_ENABLE_MEMBERS_INTENT", "true")

from mitra_bot.main import main


if __name__ == "__main__":
    main()
