package local.agentstorage;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import javax.sql.DataSource;
import com.fasterxml.jackson.core.StreamReadConstraints;
import com.fasterxml.jackson.core.StreamReadFeature;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.jackson.Jackson2ObjectMapperBuilderCustomizer;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.sqlite.SQLiteConfig;
import org.sqlite.SQLiteDataSource;

@Configuration
public class StorageConfiguration {
    @Bean
    DataSource dataSource(@Value("${storage.sqlite-path}") String name) throws IOException {
        Path path = Path.of(name).toAbsolutePath().normalize();
        Files.createDirectories(path.getParent());
        SQLiteConfig config = new SQLiteConfig();
        config.setBusyTimeout(10000);
        config.enforceForeignKeys(true);
        config.setJournalMode(SQLiteConfig.JournalMode.WAL);
        // Business transactions read state before writing. Reserve the writer before
        // reading so concurrent case updates cannot invalidate their WAL snapshot.
        config.setTransactionMode(SQLiteConfig.TransactionMode.IMMEDIATE);
        SQLiteDataSource source = new SQLiteDataSource(config);
        source.setUrl("jdbc:sqlite:" + path);
        return source;
    }

    @Bean
    Jackson2ObjectMapperBuilderCustomizer boundedJson() {
        return builder -> builder.postConfigurer(mapper -> {
            mapper.getFactory().setStreamReadConstraints(StreamReadConstraints.builder()
                .maxNestingDepth(40).maxStringLength(12 * 1024 * 1024).maxNumberLength(100).build());
            mapper.getFactory().configure(StreamReadFeature.STRICT_DUPLICATE_DETECTION.mappedFeature(), true);
        });
    }
}
