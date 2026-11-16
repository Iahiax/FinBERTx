from __future__ import annotations

from finbertx.types import SentimentScores


class FinBertEngine:
    """Lazy FinBERT inference pinned to CPU; no CUDA path is used."""

    model_name = "ProsusAI/finbert"

    def __init__(self) -> None:
        try:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "FinBERT dependencies are missing. Install requirements.txt first."
            ) from exc

        self._torch = torch
        self.device = torch.device("cpu")
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_name)
        self.model = AutoModelForSequenceClassification.from_pretrained(
            self.model_name
        )
        self.model.to(self.device)
        self.model.eval()

    def analyze(self, text: str) -> SentimentScores:
        content = text.strip()
        if not content:
            raise ValueError("Text to analyze cannot be empty.")

        encoded = self.tokenizer(
            content,
            return_tensors="pt",
            truncation=True,
            max_length=512,
        )
        encoded = {key: value.to(self.device) for key, value in encoded.items()}
        with self._torch.inference_mode():
            logits = self.model(**encoded).logits
            probabilities = self._torch.softmax(logits, dim=-1)[0].tolist()

        labels = self._labels()
        scores = {label: float(probabilities[index]) for index, label in labels.items()}
        return SentimentScores(
            positive=scores.get("positive", 0.0),
            negative=scores.get("negative", 0.0),
            neutral=scores.get("neutral", 0.0),
        )

    def _labels(self) -> dict[int, str]:
        configured = getattr(self.model.config, "id2label", {}) or {}
        normalized: dict[int, str] = {}
        for index, label in configured.items():
            try:
                numeric_index = int(index)
            except (TypeError, ValueError):
                continue
            name = str(label).strip().lower()
            if name in {"positive", "negative", "neutral"}:
                normalized[numeric_index] = name

        if set(normalized.values()) == {"positive", "negative", "neutral"}:
            return normalized
        # ProsusAI/finbert documents the stable order: 0 positive, 1 negative, 2 neutral.
        return {0: "positive", 1: "negative", 2: "neutral"}
