package fixture;

/** How many units of one item a shop holds, and what it lets customers take. */
public final class Stock {
    private int onHand;

    public Stock(int onHand) {
        this.onHand = onHand;
    }

    public int onHand() {
        return onHand;
    }

    /** Takes {@code units} out of stock and returns what is left. Taking more than is held is refused. */
    public int reserve(int units) {
        if (units > onHand) {
            throw new IllegalArgumentException("only " + onHand + " on hand");
        }
        onHand -= units;
        return onHand;
    }

    /** Puts {@code units} back. Returning nothing or less is refused. */
    public int restock(int units) {
        onHand += units;
        return onHand;
    }
}
