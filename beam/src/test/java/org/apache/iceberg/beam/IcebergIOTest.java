/*
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package org.apache.iceberg.beam;

import org.apache.avro.Schema;
import org.apache.avro.generic.GenericData;
import org.apache.avro.generic.GenericRecord;
import org.apache.beam.sdk.Pipeline;
import org.apache.beam.sdk.coders.AvroCoder;
import org.apache.beam.sdk.coders.StringUtf8Coder;
import org.apache.beam.sdk.io.AvroIO;
import org.apache.beam.sdk.io.FileIO;
import org.apache.beam.sdk.io.WriteFilesResult;
import org.apache.beam.sdk.options.PipelineOptions;
import org.apache.beam.sdk.options.PipelineOptionsFactory;
import org.apache.beam.sdk.testing.TestPipeline;
import org.apache.beam.sdk.testing.TestStream;
import org.apache.beam.sdk.transforms.Create;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.transforms.windowing.FixedWindows;
import org.apache.beam.sdk.transforms.windowing.Window;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.TimestampedValue;
import org.apache.iceberg.avro.AvroSchemaUtil;
import org.apache.iceberg.catalog.TableIdentifier;
import org.joda.time.Duration;
import org.joda.time.Instant;
import org.junit.Rule;
import org.junit.Test;

import java.util.Arrays;
import java.util.List;

public class IcebergIOTest {
    private static final List<String> SENTENCES =
        Arrays.asList(
            "Beam window 1 1",
            "Beam window 1 2",
            "Beam window 1 3",
            "Beam window 1 4",
            "Beam window 2 1",
            "Beam window 2 2");
    private static final Instant START_TIME = new Instant(0);

    private static final List<String> FIRST_WIN_WORDS = SENTENCES.subList(0, 4);
    private static final List<String> SECOND_WIN_WORDS = SENTENCES.subList(4, 6);
    private static final Duration WINDOW_DURATION = Duration.standardMinutes(1);

    @Rule
    public final transient TestPipeline pipeline = TestPipeline.create();
    final String hiveMetastoreUrl = "thrift://localhost:9083/default";

    private static final PipelineOptions options = TestPipeline.testingPipelineOptions();

    private static final String stringSchema = "{\n" +
            "\t\"type\": \"record\",\n" +
            "\t\"name\": \"Word\",\n" +
            "\t\"fields\": [{\n" +
            "\t\t\"name\": \"word\",\n" +
            "\t\t\"type\": [\"null\", \"string\"],\n" +
            "\t\t\"default\": null\n" +
            "\t}]\n" +
            "}";

    final Schema avroSchema = new Schema.Parser().parse(stringSchema);

    public static class StringToGenericRecord extends DoFn<String, GenericRecord> {
        private final Schema schema;

        public StringToGenericRecord() {
            schema = new Schema.Parser().parse(stringSchema);
        }

        @ProcessElement
        public void processElement(@Element String word, OutputReceiver<GenericRecord> out) {
            GenericRecord record = new GenericData.Record(schema);
            record.put("word", word);
            out.output(record);
        }
    }

    @Test
    public void testWriteFilesBatch() {
        final PipelineOptions options = PipelineOptionsFactory.create();
        final Pipeline p = Pipeline.create(options);

        p.getCoderRegistry().registerCoderForClass(GenericRecord.class, AvroCoder.of(avroSchema));

        PCollection<String> lines = p.apply(Create.of(SENTENCES)).setCoder(StringUtf8Coder.of());

        PCollection<GenericRecord> records = lines.apply(ParDo.of(new StringToGenericRecord()));

        final String hiveMetastoreUrl = "thrift://localhost:9083/default";
        FileIO.Write<Void, GenericRecord> avroFileIO = FileIO.<GenericRecord>write()
                .via(AvroIO.sink(avroSchema))
                .to("/tmp/fokko/")
                .withSuffix(".avro");

        WriteFilesResult<Void> filesWritten = records.apply(avroFileIO);
        org.apache.iceberg.Schema icebergSchema = AvroSchemaUtil.toIceberg(avroSchema);
        TableIdentifier name = TableIdentifier.of("default", "test_batch");

        IcebergIO.write(name, icebergSchema, hiveMetastoreUrl, filesWritten);

        p.run();
    }

    @Test
    public void testWriteFilesStreaming() {
        pipeline.getCoderRegistry().registerCoderForClass(GenericRecord.class, AvroCoder.of(avroSchema));

        TestStream<String> stringsStream =
            TestStream.create(StringUtf8Coder.of())
                .advanceWatermarkTo(START_TIME)
                .addElements(event(FIRST_WIN_WORDS.get(0), 2L))
                .advanceWatermarkTo(START_TIME.plus(Duration.standardSeconds(27L)))
                .addElements(
                    event(FIRST_WIN_WORDS.get(1), 25L),
                    event(FIRST_WIN_WORDS.get(2), 18L),
                    event(FIRST_WIN_WORDS.get(3), 26L))
                .advanceWatermarkTo(START_TIME.plus(Duration.standardSeconds(65L)))
                // This are late elements after window ends so they should not be saved
                .addElements(event(SECOND_WIN_WORDS.get(0), 67L), event(SECOND_WIN_WORDS.get(1), 68L))
                .advanceWatermarkToInfinity();

        PCollection<GenericRecord> recordsStream = pipeline
            .apply(stringsStream)
            .setCoder(StringUtf8Coder.of())
            .apply(ParDo.of(new StringToGenericRecord()));

        FileIO.Write<Void, GenericRecord> avroFileIO = FileIO.<GenericRecord>write()
            .via(AvroIO.sink(avroSchema))
            .to("/tmp/fokko/")
            .withNumShards(1)
            .withSuffix(".avro");

        // Write the record
        WriteFilesResult<Void> filesWritten = recordsStream
            .apply(Window.into(FixedWindows.of(WINDOW_DURATION)))
            .apply(avroFileIO);

        org.apache.iceberg.Schema icebergSchema = AvroSchemaUtil.toIceberg(avroSchema);
        TableIdentifier name = TableIdentifier.of("default", "test_streaming");

        IcebergIO.write(name, icebergSchema, hiveMetastoreUrl, filesWritten);

        pipeline.run(options).waitUntilFinish();
    }

    private TimestampedValue<String> event(String word, Long timestamp) {
        return TimestampedValue.of(word, START_TIME.plus(new Duration(timestamp)));
    }
}
