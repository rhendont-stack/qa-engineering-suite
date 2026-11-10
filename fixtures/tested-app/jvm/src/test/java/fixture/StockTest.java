package fixture;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.Test;

class StockTest {
    @Test
    void reservesWhatIsHeld() {
        assertEquals(7, new Stock(10).reserve(3));
    }

    @Test
    void refusesToReserveMoreThanIsHeld() {
        assertThrows(IllegalArgumentException.class, () -> new Stock(2).reserve(3));
    }

    @Test
    void restocksFourUnits() {
        new Stock(1).restock(4);
    }

    @Test
    void refusesToRestockNothing() {
        assertThrows(IllegalArgumentException.class, () -> new Stock(5).restock(0));
    }

    @Disabled("revisit after the release")
    @Test
    void refusesToReserveANegativeNumber() {
        assertThrows(IllegalArgumentException.class, () -> new Stock(5).reserve(-1));
    }
}
