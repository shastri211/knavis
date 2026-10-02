import logging
import time
import uuid

logger = logging.getLogger("mragrag")

def configure():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

def request_id():
    return uuid.uuid4().hex

def timed_event(name, **fields):
    start = time.perf_counter()
    def finish(**more):
        logger.info(
            "%s duration_ms=%.1f %s",
            name,
            (time.perf_counter() - start) * 1000,
            " ".join(f"{k}={v}" for k,v in {**fields, **more}.items())
        )
    return finish
