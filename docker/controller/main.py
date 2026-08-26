from fastapi import FastAPI, Query
import logging


logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] [%(levelname)s] [%(funcName)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/streams/started")
def stream_started(
    stream: str = Query(..., description="Stream name"),
    proxy_pod: str = Query(..., description="Proxy pod that received publish")
):
    """Accept a notification that a Proxy has started receiving a stream."""
    logger.info(
        f"[StreamsStarted] Received start event for stream '{stream}' "
        f"proxy='{proxy_pod}'"
    )
    return {
        "status": "started_event_processed",
        "stream": stream,
        "proxy_pod": proxy_pod,
    }


@app.post("/streams/ended")
def stream_ended(
    stream: str = Query(..., description="Stream name"),
    proxy_pod: str = Query(None, description="Proxy pod that ended publish"),
    generation: int = Query(None, description="Optional legacy publish generation")
):
    """Accept a notification that a Proxy has stopped receiving a stream."""
    logger.info(
        f"[StreamsEnded] Received end event for stream '{stream}' "
        f"proxy='{proxy_pod}' generation='{generation}'"
    )
    return {"status": "ended", "stream": stream}
