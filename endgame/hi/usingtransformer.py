#!/usr/bin/env python3
"""
social_feed_classifier.py

Continuously polls the optibook Exchange social media feed API and classifies incoming posts
using a transformer-based classifier. Supports runtime model switching by typing a new
model identifier into stdin (e.g. "facebook/bart-large-mnli" or a local folder path).

Main features:
- Zero-shot classification via HuggingFace pipeline (preferred)
- Fallback to instruction-tuned causal LLM classification when zero-shot pipeline isn't available
- Runtime model switching (type model name and press Enter)
- Configurable labels, multi_label, device selection, and output file
"""

import argparse
import json
import threading
import time
import sys
import csv
import logging
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime

from transformers import (
    pipeline,
    AutoTokenizer,
    AutoModelForCausalLM,
    Pipeline,
    logging as hf_logging,
)
import torch

# optibook imports (same as in your notebook)
from optibook.synchronous_client import Exchange
from optibook.common_types import SocialMediaFeed

# Silence transformers warnings if desired
hf_logging.set_verbosity_error()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("social-feed-classifier")

# Thread-safety for model swapping
from threading import Lock

MODEL_LOCK = Lock()


class ClassifierWrapper:
    """
    Wraps either a HF zero-shot pipeline or a causal LM-based instruction classifier,
    and provides a unified classify(text, labels, multi_label) interface.
    """

    def __init__(self, model_name: str, device: int = -1, multi_label: bool = False):
        """
        model_name: HuggingFace repo id or local path
        device: -1 for CPU, otherwise GPU ordinal (int)
        """
        self.model_name = model_name
        self.device = device
        self.multi_label = multi_label

        self.pipeline: Pipeline | None = None
        self.llm_tokenizer = None
        self.llm_model = None
        self.mode = None  # "zero-shot" or "llm"

        # load model at init
        self._load_model(model_name)

    def _load_model(self, model_name: str):
        logger.info("Loading model '%s' (device=%s)...", model_name, self.device)
        # free old resources first
        try:
            with MODEL_LOCK:
                # Try to create a zero-shot pipeline first
                try:
                    self.pipeline = pipeline(
                        "zero-shot-classification",
                        model=model_name,
                        device=self.device,
                        truncation=True,
                    )
                    self.mode = "zero-shot"
                    logger.info("Loaded zero-shot pipeline with model '%s'", model_name)
                    # clear any llm objects
                    self.llm_model = None
                    self.llm_tokenizer = None
                    self.model_name = model_name
                    return
                except Exception as e:
                    logger.info("Zero-shot pipeline load failed: %s", e)
                    logger.info("Attempting to load model as causal LLM (instruction-tuned fallback).")

                # Fall back to causal LLM approach (for custom instruction-tuned models)
                try:
                    self.llm_tokenizer = AutoTokenizer.from_pretrained(model_name)
                    self.llm_model = AutoModelForCausalLM.from_pretrained(model_name)
                    # put model to device if GPU is available and requested
                    if self.device is not None and self.device >= 0 and torch.cuda.is_available():
                        self.llm_model.to(self.device)
                    self.pipeline = None
                    self.mode = "llm"
                    self.model_name = model_name
                    logger.info("Loaded causal LLM model '%s' as fallback.", model_name)
                    return
                except Exception as e:
                    logger.exception("Failed to load model '%s' as causal LLM too: %s", model_name, e)
                    raise RuntimeError(f"Cannot load model '{model_name}' as zero-shot or causal LLM.") from e

        except Exception as e:
            logger.exception("Model loading error: %s", e)
            raise

    def reload(self, model_name: str):
        """Replace current model with new model (thread-safe)."""
        with MODEL_LOCK:
            if model_name == self.model_name:
                logger.info("Requested model is same as current model '%s' -> no reload", model_name)
                return
            self._load_model(model_name)

    def classify(self, text: str, labels: List[str], multi_label: bool | None = None) -> Dict[str, Any]:
        """
        Classify text. Returns the pipeline output or a structured fallback dict for LLM mode.
        """
        if multi_label is None:
            multi_label = self.multi_label

        with MODEL_LOCK:
            if self.mode == "zero-shot" and self.pipeline is not None:
                t0 = time.time()
                res = self.pipeline(text, labels, multi_label=multi_label)
                t_taken = time.time() - t0
                return {"model": self.model_name, "mode": self.mode, "time": t_taken, "result": res}
            elif self.mode == "llm" and self.llm_model is not None and self.llm_tokenizer is not None:
                # Basic instruction prompt: ask model to output label only
                prompt = (
                    "Classify the following text into one of these categories: "
                    + ", ".join(labels)
                    + "\n\n"
                    + f"Text: \"{text}\"\n\nAnswer with just the category name."
                )
                inputs = self.llm_tokenizer(prompt, return_tensors="pt", truncation=True)
                # Move inputs to device if model on GPU
                if self.device is not None and self.device >= 0 and torch.cuda.is_available():
                    inputs = {k: v.to(self.device) for k, v in inputs.items()}
                    self.llm_model.to(self.device)
                t0 = time.time()
                outputs = self.llm_model.generate(
                    **inputs,
                    max_new_tokens=32,
                    temperature=0.0,
                    do_sample=False,
                    pad_token_id=self.llm_tokenizer.eos_token_id,
                )
                t_taken = time.time() - t0
                response = self.llm_tokenizer.decode(outputs[0][inputs["input_ids"].shape[1] :], skip_special_tokens=True)
                return {
                    "model": self.model_name,
                    "mode": self.mode,
                    "time": t_taken,
                    "result": {"sequence": text, "predicted_label": response.strip()},
                }
            else:
                raise RuntimeError("No model loaded")

    def is_zero_shot(self) -> bool:
        return self.mode == "zero-shot"


def read_config(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with p.open() as f:
        return json.load(f)


def input_thread_fn(wrapper: ClassifierWrapper):
    """
    Listens for lines on stdin. If a line looks like a model id, attempts to reload model.
    Special commands:
      - reload <model_id>
      - model <model_id>
      - quit / exit
      - status
    """
    logger.info("Input thread started. Type a model id to switch models, or 'quit' to exit.")
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                # EOF reached
                break
            line = line.strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit"):
                logger.info("Exit requested from stdin.")
                # propagate exit by raising SystemExit
                raise SystemExit
            if line.lower() == "status":
                logger.info("Current model: %s (mode=%s)", wrapper.model_name, wrapper.mode)
                continue
            # support "reload <model>", "model <model>" or direct model id
            if line.startswith("reload ") or line.startswith("model "):
                parts = line.split(maxsplit=1)
                model_id = parts[1].strip()
            else:
                model_id = line
            try:
                wrapper.reload(model_id)
            except Exception as e:
                logger.error("Failed to reload model '%s': %s", model_id, e)
            continue
        except SystemExit:
            # allow top-level to handle
            raise
        except Exception as e:
            logger.exception("Input thread error: %s", e)
            time.sleep(0.5)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Social feed classifier")
    parser.add_argument("--config", "-c", default="config.json", help="Path to JSON config file")
    parser.add_argument("--output", "-o", default="classified_feeds.csv", help="CSV file to append results to")
    parser.add_argument("--device", "-d", default="-1", help="Device: -1 for CPU, or GPU ordinal (0,1,..)")
    parser.add_argument("--poll-interval", type=float, default=3.0, help="Seconds between polls")
    args = parser.parse_args(argv)

    cfg = read_config(args.config)

    model_name = cfg.get("model", "facebook/bart-large-mnli")
    labels = cfg.get("labels", ["Positive", "Negative"])
    multi_label = cfg.get("multi_label", False)
    device = int(args.device)

    # Initialize classifier
    classifier = ClassifierWrapper(model_name=model_name, device=device, multi_label=multi_label)

    # Start stdin input thread for runtime model switching
    t = threading.Thread(target=input_thread_fn, args=(classifier,), daemon=True)
    t.start()

    # Connect to Exchange
    exchange = Exchange()
    try:
        exchange.connect()
    except Exception as e:
        logger.exception("Failed to connect to Exchange: %s", e)
        # still continue - maybe offline testing
    logger.info("Connected to Exchange (or attempted). Polling for social media feeds every %.1fs", args.poll_interval)

    # Open CSV for appending results
    out_path = Path(args.output)
    write_header = not out_path.exists()
    csv_file = open(out_path, "a", newline="", encoding="utf-8")
    csv_writer = csv.writer(csv_file)
    if write_header:
        csv_writer.writerow(["timestamp", "feed_timestamp", "post", "model", "mode", "labels", "scores", "time_taken", "detected_label"])

    try:
        while True:
            try:
                social_feeds: List[SocialMediaFeed] = exchange.poll_new_social_media_feeds()
            except Exception as e:
                logger.exception("Error while polling feeds: %s", e)
                social_feeds = []

            if not social_feeds:
                logger.debug("No new feeds")
            else:
                for feed in social_feeds:
                    try:
                        text = feed.post or ""
                        logger.info("Feed @%s: %s", feed.timestamp, text)
                        t0 = time.time()
                        res = classifier.classify(text, labels, multi_label=multi_label)
                        t_total = time.time() - t0

                        # Normalize results for CSV: handle both zero-shot pipeline and LLM fallback
                        detected_label = ""
                        label_list = []
                        score_list = []
                        if res["mode"] == "zero-shot":
                            r = res["result"]
                            # pipeline may return 'labels' and 'scores'
                            label_list = r.get("labels", [])
                            score_list = r.get("scores", [])
                            # If single-label classification, labels is ordered best-first.
                            detected_label = label_list[0] if label_list else ""
                        else:
                            # LLM fallback returns predicted_label
                            r = res["result"]
                            detected_label = r.get("predicted_label", "")
                            label_list = [detected_label]
                            score_list = [1.0]

                        # Print nicely
                        logger.info(
                            "Classified -> model=%s mode=%s time=%.3fs detected=%s",
                            res["model"],
                            res["mode"],
                            res["time"],
                            detected_label,
                        )

                        # Append CSV row
                        csv_writer.writerow(
                            [
                                datetime.utcnow().isoformat(),
                                getattr(feed, "timestamp", ""),
                                text,
                                res["model"],
                                res["mode"],
                                ";".join(label_list),
                                ";".join([f"{s:.4f}" for s in score_list]),
                                f"{res['time']:.4f}",
                                detected_label,
                            ]
                        )
                        csv_file.flush()
                    except Exception as e:
                        logger.exception("Failed to classify feed: %s", e)

            time.sleep(args.poll_interval)

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received — exiting.")
    except SystemExit:
        logger.info("Exit requested — shutting down.")
    finally:
        try:
            csv_file.close()
        except Exception:
            pass
        try:
            exchange.disconnect()
        except Exception:
            pass
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()