"""
Auditor & Quality Gate Agent Module
-----------------------------------
Performs automated verification on extracted text and tables:
1. Confidence thresholds checking (Average & Critical fields)
2. Tabular arithmetic integrity checking (Qty * Rate == Total Amount)
3. Zero ('0') vs Letter ('O') context anomaly audit
4. Human-in-the-Loop (HITL) trigger escalation
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("ingest.auditor")


@dataclass
class AuditIssue:
    issue_type: str        # 'low_confidence', 'arithmetic_mismatch', 'ambiguity', 'missing_field'
    severity: str          # 'critical', 'warning', 'info'
    description: str
    target_field: Optional[str] = None
    bbox: Optional[List[int]] = None
    expected_value: Optional[str] = None
    detected_value: Optional[str] = None


@dataclass
class AuditReport:
    is_valid: bool
    requires_hitl: bool
    overall_confidence: float
    total_checks: int
    issues: List[AuditIssue] = field(default_factory=list)

    def summary_dict(self) -> Dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "requires_hitl": self.requires_hitl,
            "overall_confidence": round(self.overall_confidence, 3),
            "critical_issues_count": sum(1 for i in self.issues if i.severity == "critical"),
            "warning_issues_count": sum(1 for i in self.issues if i.severity == "warning"),
            "issues": [
                {
                    "type": i.issue_type,
                    "severity": i.severity,
                    "description": i.description,
                    "field": i.target_field,
                    "detected": i.detected_value,
                    "expected": i.expected_value,
                }
                for i in self.issues
            ]
        }


def _parse_numeric(val: Any) -> Optional[float]:
    """Clean and parse numeric float values from OCR strings."""
    if val is None:
        return None
    s = str(val).strip().replace(",", "").replace("$", "").replace("₹", "").replace("€", "")
    # Remove leading/trailing non-numeric chars
    match = re.search(r"[-+]?\d*\.?\d+", s)
    if match:
        try:
            return float(match.group())
        except ValueError:
            return None
    return None


def verify_table_arithmetic(table_rows: List[Dict[str, Any]]) -> List[AuditIssue]:
    """Verify line-item arithmetic consistency: Quantity * Unit Price ≈ Total Amount."""
    issues = []
    if not table_rows:
        return issues

    for idx, row in enumerate(table_rows):
        # Look for quantity, rate/unit price, and total amount keys
        qty = None
        rate = None
        total = None

        for k, v in row.items():
            k_lower = str(k).lower()
            if any(term in k_lower for term in ["qty", "quantity", "units"]):
                qty = _parse_numeric(v)
            elif any(term in k_lower for term in ["rate", "price", "unit price", "cost"]):
                rate = _parse_numeric(v)
            elif any(term in k_lower for term in ["total", "amount", "est. value", "value"]):
                total = _parse_numeric(v)

        if qty is not None and rate is not None and total is not None:
            expected = qty * rate
            diff = abs(expected - total)
            tolerance = max(0.5, total * 0.02)  # 2% tolerance for rounding/taxes

            if diff > tolerance:
                issues.append(AuditIssue(
                    issue_type="arithmetic_mismatch",
                    severity="critical",
                    description=f"Row {idx + 1}: Qty ({qty}) * Rate ({rate}) = {expected:.2f}, but detected Total is {total:.2f} (diff: {diff:.2f})",
                    target_field=f"table_row_{idx + 1}",
                    expected_value=f"{expected:.2f}",
                    detected_value=f"{total:.2f}"
                ))

    return issues


def audit_ocr_confidence(
    scores: List[float],
    min_avg_threshold: float = 0.85,
    min_single_threshold: float = 0.50
) -> Tuple[float, List[AuditIssue]]:
    """Audit OCR confidence scores across extracted tokens/regions."""
    issues = []
    if not scores:
        return 0.0, [AuditIssue(
            issue_type="low_confidence",
            severity="critical",
            description="No OCR confidence scores recorded (empty page or failed extraction)."
        )]

    avg_conf = sum(scores) / len(scores)

    if avg_conf < min_avg_threshold:
        issues.append(AuditIssue(
            issue_type="low_confidence",
            severity="critical",
            description=f"Average document OCR confidence ({avg_conf:.2f}) is below acceptable threshold ({min_avg_threshold:.2f}).",
            detected_value=f"{avg_conf:.3f}",
            expected_value=f">={min_avg_threshold:.2f}"
        ))

    low_score_count = sum(1 for s in scores if s < min_single_threshold)
    if low_score_count > len(scores) * 0.15:
        issues.append(AuditIssue(
            issue_type="low_confidence",
            severity="warning",
            description=f"{low_score_count} tokens ({low_score_count/len(scores)*100:.1f}%) have low confidence (< {min_single_threshold}).",
            detected_value=str(low_score_count),
        ))

    return avg_conf, issues


def audit_ocr_ambiguities(texts: List[str]) -> List[AuditIssue]:
    """Audit potential 0 vs O/o letter-number confusion in numeric codes."""
    issues = []
    # Patterns where a letter O appears inside a numeric sequence
    pattern_letter_in_number = re.compile(r"\b\d+[Oo]\d+\b")
    # Patterns where digit 0 appears inside pure alphabetical words
    pattern_digit_in_word = re.compile(r"\b[A-Za-z]+0[A-Za-z]+\b")

    for txt in texts:
        for match in pattern_letter_in_number.findall(txt):
            issues.append(AuditIssue(
                issue_type="ambiguity",
                severity="warning",
                description=f"Possible '0' misread as 'O' in numeric sequence: '{match}'",
                detected_value=match,
                expected_value=match.replace("O", "0").replace("o", "0")
            ))
        for match in pattern_digit_in_word.findall(txt):
            issues.append(AuditIssue(
                issue_type="ambiguity",
                severity="warning",
                description=f"Possible 'O' misread as '0' in word: '{match}'",
                detected_value=match,
                expected_value=match.replace("0", "O")
            ))

    return issues


def run_document_audit(
    texts: List[str],
    scores: List[float],
    tables_data: Optional[List[List[Dict[str, Any]]]] = None,
    min_confidence: float = 0.85
) -> AuditReport:
    """Run full audit suite and decide if Human-in-the-Loop (HITL) review is mandatory."""
    all_issues: List[AuditIssue] = []

    # 1. Check confidence
    avg_conf, conf_issues = audit_ocr_confidence(scores, min_avg_threshold=min_confidence)
    all_issues.extend(conf_issues)

    # 2. Check 0 vs O ambiguities
    ambiguity_issues = audit_ocr_ambiguities(texts)
    all_issues.extend(ambiguity_issues)

    # 3. Check table arithmetic if structured tables are supplied
    if tables_data:
        for _t_idx, table in enumerate(tables_data):
            math_issues = verify_table_arithmetic(table)
            all_issues.extend(math_issues)

    # Escalation policy:
    # Any critical issue triggers mandatory HITL
    # More than 3 warning issues triggers HITL
    critical_count = sum(1 for i in all_issues if i.severity == "critical")
    warning_count = sum(1 for i in all_issues if i.severity == "warning")

    requires_hitl = (critical_count > 0) or (warning_count >= 3)
    is_valid = (critical_count == 0) and (warning_count == 0)

    return AuditReport(
        is_valid=is_valid,
        requires_hitl=requires_hitl,
        overall_confidence=avg_conf,
        total_checks=len(scores) + (len(tables_data) if tables_data else 0),
        issues=all_issues
    )
