package corpus;

public final class Label {
    private Label() {
    }

    public static String of(String country) {
        if ("ES".equals(country)) {
            return "domestic";
        }
        return "international";
    }
}
