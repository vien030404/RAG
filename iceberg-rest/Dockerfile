# ============================================================================
# Iceberg REST Catalog (Apache fixture) + PostgreSQL JDBC driver.
#
# The stock apache/iceberg-rest-fixture image ships only the SQLite driver and
# launches with `java -jar`. To back the JDBC catalog with Postgres we add the
# Postgres driver to the classpath and start the server's main class directly.
# ============================================================================
ARG ICEBERG_REST_VERSION=1.9.2
FROM apache/iceberg-rest-fixture:${ICEBERG_REST_VERSION}

ARG POSTGRES_JDBC_VERSION=42.7.4
USER root
ADD https://repo1.maven.org/maven2/org/postgresql/postgresql/${POSTGRES_JDBC_VERSION}/postgresql-${POSTGRES_JDBC_VERSION}.jar \
    /usr/lib/iceberg-rest/postgresql.jar
RUN chmod 0644 /usr/lib/iceberg-rest/postgresql.jar
USER iceberg

WORKDIR /usr/lib/iceberg-rest
# Same main class as the stock image, but with the Postgres driver on the classpath.
CMD ["java", "-cp", "iceberg-rest-adapter.jar:postgresql.jar", "org.apache.iceberg.rest.RESTCatalogServer"]
