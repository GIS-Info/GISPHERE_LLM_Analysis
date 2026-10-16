"""Check Langfuse credentials and send a test trace without processing table rows."""
import logging

from ..core.observability import get_tracing_client, observation, flush_traces


def main():
    logging.basicConfig(level=logging.INFO)
    client = get_tracing_client()
    if client is None:
        print("Langfuse disabled. Install dependencies and configure keys/langfuse.env.")
        return 1
    try:
        if not client.auth_check():
            print("Langfuse authentication failed. Check project keys and regional base URL.")
            return 1
        with observation("langfuse-connection-check") as span:
            span.update(output="Configuration check completed; no model or table operations.")
        flush_traces()
        print("Authentication succeeded; test trace submitted. Check Langfuse Traces.")
        return 0
    except Exception as exc:
        print(f"Langfuse check failed ({type(exc).__name__}). Check credentials and connectivity.")
        return 1
    finally:
        flush_traces()


if __name__ == "__main__":
    raise SystemExit(main())
