"""Pure policy tests; no fixture files are created or deleted."""
import unittest
from artifact_retention import decision,in_scope

class RetentionTests(unittest.TestCase):
    def test_tracked_and_published_payloads_are_never_candidates(self):
        self.assertEqual(decision('docs/validation/old/proof.bin',99,True),'keep:tracked')
        for p in ('web/bridge/etm/downloads/etm-bridge-win64.zip','etm/firmware/stm32h743_cdc_trace/fw.elf',
                  'etm/records/capture-user/capture.kvdat','etm/vendor/OpenCSD-1.8.3/decoder/build/template.cmake'):
            self.assertFalse(in_scope(p));self.assertTrue(decision(p,10**9).startswith('keep:'))

    def test_regression_and_checkout_protection_precedes_bulk_output_rules(self):
        for p in ('tmp/perf-baseline/web/tmp/x.zip',
                  'docs/validation/20261018-ds-review-bb2f8ee-6ff02745/data/records/capture-be1e78730b7f486a9e27d5911cd4a434/events.jsonl'):
            self.assertTrue(decision(p,10**10).startswith('keep:'))
        self.assertTrue(decision('tmp/p6a-1s/physical/segments/segment-1.kvdat',100,
                               extra_protected=('tmp/p6a-1s/physical/segments/segment-1.kvdat',)).startswith('keep:'))
        self.assertTrue(decision('tmp/p6a-1s/physical/segments/segment-2.kvdat',100).startswith('delete:'))

    def test_original_reports_scripts_and_small_results_survive(self):
        for name in ('report.md','reproduce.py','run.ps1','validate.mjs','result.json','stderr.log','metrics.csv','view.png'):
            self.assertTrue(decision('docs/validation/run/'+name,1024).startswith('keep:'),name)
        self.assertTrue(decision('tmp/source/native_probe.cpp',8192).startswith('keep:'))

    def test_copied_dependency_scripts_do_not_overwhelm_original_scripts(self):
        for p in ('docs/validation/run/portable-native12/runtime/elftools/a.py',
                  'web/tmp/etm-package-12345678/dist/a.py','docs/validation/run/snapshot/tools/a.py'):
            self.assertTrue(decision(p,500).startswith('delete:'))

    def test_giant_outputs_and_stale_test_caches_are_candidates(self):
        for p in ('tmp/p6a-1s/events-record/events.jsonl','docs/validation/run/reading.c',
                  'docs/validation/run/capture.kvdat','docs/validation/run/big.json',
                  'etm/records/.p4_test_cache/key/store/generations/g/store.bin'):
            self.assertTrue(decision(p,10**9).startswith('delete:'),p)

if __name__=='__main__':unittest.main()
