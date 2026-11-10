FROM registry-1.docker.io/library/maven:3.9-eclipse-temurin-21 AS build
WORKDIR /build
COPY agent_storage_java/pom.xml ./pom.xml
RUN mvn -B -ntp dependency:go-offline
COPY agent_storage_java/src ./src
RUN mvn -B -ntp -DskipTests package
COPY agent_platform/HealthProbe.java /tmp/HealthProbe.java
RUN mkdir -p /tmp/healthcheck && javac --release 21 -d /tmp/healthcheck /tmp/HealthProbe.java

FROM registry-1.docker.io/library/eclipse-temurin:21-jre
WORKDIR /app
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir -p /data && chown app:app /data
COPY --from=build /build/target/*.jar /app/storage.jar
COPY --from=build /tmp/healthcheck /opt/healthcheck
USER 10001:10001
EXPOSE 8780
ENTRYPOINT ["java", "-jar", "/app/storage.jar"]
