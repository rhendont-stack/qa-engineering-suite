package corpus;

import static org.junit.jupiter.api.Assertions.assertEquals;

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
}
