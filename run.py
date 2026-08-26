"""Entry point: open the local app."""
import logging
from cdek.web.app import serve

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    serve()
