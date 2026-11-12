package corpus;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import org.junit.jupiter.api.Test;

class ShippingTest {
    @Test
    void aLightParcelCostsTheBaseRate() {
        assertEquals(5.0, Shipping.cost(2, false));
    }

    @Test
    void aHeavyParcelPaysPerExtraKilo() {
        assertEquals(7.0, Shipping.cost(14, false));
    }

    @Test
    void expressDoublesTheCost() {
        assertEquals(10.0, Shipping.cost(2, true));
    }

    @Test
    void aWeightOfZeroOrLessIsRefused() {
        assertThrows(IllegalArgumentException.class, () -> Shipping.cost(0, false));
        assertThrows(IllegalArgumentException.class, () -> Shipping.cost(-1, false));
    }
}
