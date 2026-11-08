package corpus;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.Test;

class MixedTest {
    @Test
    void passesOnAddition() {
        assertEquals(2, 1 + 1);
    }

    @Test
    void passesOnStringLength() {
        assertEquals(5, "junit".length());
    }

    @Test
    void failsAnAssertion() {
        assertEquals(5, 2 + 2);
    }

    @Test
    void throwsAnException() {
        throw new IllegalStateException("boom");
    }

    @Disabled("skipped on purpose")
    @Test
    void isDisabled() {
        assertEquals(1, 2);
    }
}
