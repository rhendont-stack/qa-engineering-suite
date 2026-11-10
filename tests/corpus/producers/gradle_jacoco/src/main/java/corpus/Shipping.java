package corpus;

public final class Shipping {
    private Shipping() {
    }

    public static double cost(double weightKg, boolean express) {
        if (weightKg <= 0) {
            throw new IllegalArgumentException("weight must be positive");
        }
        double cost = 5.0;
        if (weightKg > 10) {
            cost += (weightKg - 10) * 0.5;
        }
        if (express) {
            cost *= 2;
        }
        return cost;
    }

    public static String label(String country) {
        if ("ES".equals(country)) {
            return "domestic";
        }
        return "international";
    }
}
