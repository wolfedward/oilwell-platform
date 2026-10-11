"""Held-out condition metrics with explicit unknown predictions."""
from .condition_data import fail


def evaluate_predictions(rows, guesses, labels):
    """Guesses are (label or unknown, rejection reason) in test-row order."""
    if len(rows) != len(guesses):
        fail("prediction count differs from test rows")
    columns = list(labels) + ["unknown"]
    matrix = [[0 for _ in columns] for _ in labels]
    predictions = []
    for row, (predicted, reason) in zip(rows, guesses):
        if row["label"] not in labels or predicted not in columns:
            fail("evaluation label outside class set")
        matrix[labels.index(row["label"])][columns.index(predicted)] += 1
        predictions.append({"sample_id": row["sample_id"], "true_label": row["label"],
                            "label": predicted, "reason": reason})
    per_class = {}
    for index, label in enumerate(labels):
        tp = matrix[index][index]
        predicted_count = sum(line[index] for line in matrix)
        actual_count = sum(matrix[index])
        precision = tp / predicted_count if predicted_count else 0.0
        recall = tp / actual_count if actual_count else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_class[label] = {"precision": precision, "recall": recall, "F1": f1, "support": actual_count}
    accepted = sum(sum(line[:-1]) for line in matrix)
    correct = sum(matrix[i][i] for i in range(len(labels)))
    total = len(rows)
    return {"labels": list(labels), "prediction_columns": columns,
            "confusion_matrix": matrix, "per_class": per_class,
            "macro_F1": sum(item["F1"] for item in per_class.values()) / len(labels),
            "accepted_count": accepted, "unknown_count": total - accepted,
            "coverage": accepted / total, "accuracy": correct / total,
            "accepted_accuracy": correct / accepted if accepted else None,
            "predictions": predictions}
