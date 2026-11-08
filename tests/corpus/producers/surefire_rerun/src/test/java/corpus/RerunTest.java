package corpus;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

class RerunTest {
    // Counts executions of the flaky test across Surefire's reruns in one JVM.
    private static int flakyRuns = 0;

    @Test
    void passes() {
        assertEquals(2, 1 + 1);
    }

    @Test
    void failsFirstAndPassesOnRerun() {
        flakyRuns += 1;
        assertTrue(flakyRuns > 1, "first run fails");
    }

    @Test
    void alwaysFails() {
        assertEquals(5, 2 + 2);
    }
}
