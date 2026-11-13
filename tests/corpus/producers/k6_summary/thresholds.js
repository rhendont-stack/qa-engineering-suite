// The k6 corpus sample: no request at all, one custom Trend fed a constant, and two thresholds on it,
// one that holds (p(95) of 50 is under 100) and one that does not (an average of 50 is not under 10).
// handleSummary writes the object k6 passes it, which is what `qa run --report k6:...` reads.
import { Trend } from 'k6/metrics';

const sample = new Trend('qa_sample_ms');

export const options = {
  vus: 1,
  iterations: 5,
  thresholds: {
    qa_sample_ms: [
      { threshold: 'p(95)<100', abortOnFail: true },
      { threshold: 'avg<10', abortOnFail: true },
    ],
  },
};

export default function () {
  sample.add(50);
}

export function handleSummary(data) {
  const text = JSON.stringify(data, null, 2) + '\n';
  return { '/qa-evidence/k6-summary.json': text, stdout: text };
}
