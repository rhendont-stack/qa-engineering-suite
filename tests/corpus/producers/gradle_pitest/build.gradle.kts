plugins {
    java
    id("info.solidsoft.pitest") version "1.19.0"
}

repositories {
    mavenCentral()
}

dependencies {
    testImplementation(platform("org.junit:junit-bom:5.14.4"))
    testImplementation("org.junit.jupiter:junit-jupiter")
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")
}

tasks.test {
    useJUnitPlatform()
}

pitest {
    pitestVersion.set("1.30.2")
    junit5PluginVersion.set("1.2.3")
    targetClasses.set(listOf("corpus.*"))
    outputFormats.set(listOf("XML"))
    timestampedReports.set(false)
    threads.set(1)
    exportLineCoverage.set(false)
}
