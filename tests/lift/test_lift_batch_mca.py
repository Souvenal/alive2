import importlib.util
import json
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Import the module under test
SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "scripts"
    / "lift_batch_mca.py"
)
SPEC = importlib.util.spec_from_file_location("lift_batch_mca", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


@pytest.fixture
def tmp_dir(tmp_path):
    """Create a temporary directory for tests."""
    return tmp_path


@pytest.fixture
def mock_compile_commands(tmp_dir):
    """Create a mock compile_commands.json file."""
    cc_data = [
        {
            "directory": "/test/dir",
            "file": "test1.c",
            "output": "test1.o",
            "arguments": ["clang", "-target", "aarch64-linux-gnu", "-O2", "-c", "test1.c", "-o", "test1.o"]
        },
        {
            "directory": "/test/dir",
            "file": "test2.c",
            "output": "test2.o",
            "arguments": ["clang", "-target", "aarch64-linux-gnu", "-O2", "-c", "test2.c", "-o", "test2.o"]
        }
    ]
    cc_path = tmp_dir / "compile_commands.json"
    cc_path.write_text(json.dumps(cc_data), encoding="utf-8")
    return cc_path


class TestLoadDb:
    """Test database loading."""
    
    def test_load_valid_json(self, tmp_dir):
        """Test loading valid JSON file."""
        cc_data = [{"file": "test.c"}]
        cc_path = tmp_dir / "cc.json"
        cc_path.write_text(json.dumps(cc_data), encoding="utf-8")
        
        result = tool.load_db(str(cc_path))
        assert result == cc_data
    
    def test_load_invalid_json(self, tmp_dir):
        """Test loading invalid JSON file."""
        cc_path = tmp_dir / "cc.json"
        cc_path.write_text("invalid json", encoding="utf-8")
        
        with pytest.raises(json.JSONDecodeError):
            tool.load_db(str(cc_path))
    
    def test_load_nonexistent_file(self, tmp_dir):
        """Test loading non-existent file."""
        with pytest.raises(FileNotFoundError):
            tool.load_db(str(tmp_dir / "nonexistent.json"))


class TestGetArgs:
    """Test argument extraction."""
    
    def test_arguments_field(self):
        """Test extracting from 'arguments' field."""
        entry = {"arguments": ["clang", "-O2", "-c", "test.c"]}
        result = tool.get_args(entry)
        assert result == ["clang", "-O2", "-c", "test.c"]
    
    def test_command_field(self):
        """Test extracting from 'command' field."""
        entry = {"command": "clang -O2 -c test.c"}
        result = tool.get_args(entry)
        assert result == ["clang", "-O2", "-c", "test.c"]
    
    def test_no_arguments_or_command(self):
        """Test error when neither field exists."""
        entry = {"file": "test.c"}
        with pytest.raises(ValueError, match="no 'arguments' or 'command'"):
            tool.get_args(entry)


class TestValidateClangCompiler:
    """Test compiler validation."""
    
    def test_all_clang(self):
        """Test validation passes with all clang entries."""
        db = [
            {"arguments": ["clang", "-c", "test.c"]},
            {"arguments": ["clang++", "-c", "test.cpp"]},
        ]
        assert tool.validate_clang_compiler(db) is True
    
    def test_non_clang_compiler(self):
        """Test validation fails with non-clang compiler."""
        db = [
            {"arguments": ["gcc", "-c", "test.c"], "file": "test.c"},
        ]
        assert tool.validate_clang_compiler(db) is False
    
    def test_empty_database(self):
        """Test validation passes with empty database."""
        assert tool.validate_clang_compiler([]) is True


class TestProcessEntry:
    """Test entry processing."""
    
    def test_missing_output(self, tmp_dir):
        """Test skip when output is missing."""
        args = MagicMock()
        args.cc = None
        
        entry = {
            "directory": "/test",
            "file": "test.c",
            "arguments": ["clang", "-c", "test.c"]
        }
        log_fh = MagicMock()
        
        result = tool.process_entry(entry, args, tmp_dir, log_fh, 0, 1)
        assert result is None
    
    def test_missing_source_file(self, tmp_dir):
        """Test skip when source file is missing."""
        args = MagicMock()
        args.cc = None
        
        entry = {
            "directory": "/test",
            "file": "nonexistent.c",
            "output": "test.o",
            "arguments": ["clang", "-c", "nonexistent.c"]
        }
        log_fh = MagicMock()
        
        result = tool.process_entry(entry, args, tmp_dir, log_fh, 0, 1)
        assert result is None


class TestGenerateBatchSummary:
    """Test summary generation."""
    
    def test_successful_summary(self, tmp_dir):
        """Test summary with successful entries."""
        successful = [
            ("test1", tmp_dir / "build1", tmp_dir / "report1", Path("/test/test1.c")),
            ("test2", tmp_dir / "build2", tmp_dir / "report2", Path("/test/test2.c")),
        ]
        failed = []
        
        # Create mock improved summary
        report_dir = tmp_dir / "report1"
        report_dir.mkdir(parents=True, exist_ok=True)
        summary_path = report_dir / "test1.improved-summary.txt"
        summary_path.write_text(
            "Improved fragments\n==================\n\ntest1 ARM64 improved:\n  - fragment_1\n",
            encoding="utf-8"
        )
        
        result = tool.generate_batch_summary(successful, failed, tmp_dir)
        assert result.exists()
        
        content = result.read_text(encoding="utf-8")
        assert "Successful: 2" in content
        assert "Failed: 0" in content
        assert "test1" in content
        assert "test2" in content
        assert "Improved fragments found:" in content
    
    def test_failed_summary(self, tmp_dir):
        """Test summary with failed entries."""
        successful = []
        failed = [("test1", "processing failed")]
        
        result = tool.generate_batch_summary(successful, failed, tmp_dir)
        assert result.exists()
        
        content = result.read_text(encoding="utf-8")
        assert "Successful: 0" in content
        assert "Failed: 1" in content
        assert "test1: processing failed" in content


class TestMain:
    """Test main function."""
    
    def test_invalid_iterations(self, monkeypatch):
        """Test exit with invalid iterations."""
        monkeypatch.setattr(sys, "argv", [
            "lift_batch_mca.py",
            "cc.json",
            "--mca-iterations", "0"
        ])
        
        with pytest.raises(SystemExit) as exc_info:
            tool.main()
        assert exc_info.value.code == 1


class TestExtractEntryCflags:
    """Test extract_entry_cflags."""
    
    def test_driver_entry_with_flags(self):
        """Test extracting flags from a driver entry."""
        entry = {
            "file": "/src/test.c",
            "output": "test.o",
            "arguments": [
                "clang", "-target", "aarch64-linux-gnu", "-O2",
                "-I/usr/include", "-DFOO=1", "-Wall",
                "-c", "/src/test.c", "-o", "test.o",
            ],
        }
        result = tool.extract_entry_cflags(entry)
        assert "-I/usr/include" in result
        assert "-DFOO=1" in result
        assert "-Wall" in result
        # These should be stripped
        assert "clang" not in result
        assert "-c" not in result
        assert "-o" not in result
        assert "test.o" not in result
        assert "/src/test.c" not in result
    
    def test_cc1_entry(self):
        """Test extracting flags from a -cc1 entry."""
        entry = {
            "file": "/src/test.c",
            "arguments": [
                "clang", "-cc1", "-I/usr/include", "-DFOO=1", "-O2",
                "/src/test.c",
            ],
        }
        result = tool.extract_entry_cflags(entry)
        assert "-I/usr/include" in result
        assert "-DFOO=1" in result
        assert "-O2" in result
    
    def test_minimal_entry(self):
        """Test extracting flags from a minimal entry."""
        entry = {
            "file": "test.c",
            "arguments": ["clang", "-c", "test.c", "-o", "test.o"],
        }
        result = tool.extract_entry_cflags(entry)
        assert result == []


class TestCflagsPassthrough:
    """Test that entry CFLAGS are properly passed to _run_pipeline."""
    
    def test_cflags_merged_with_entry_flags(self, monkeypatch, tmp_dir):
        """Test that entry flags are merged into CFLAGS during processing."""
        import conftest as _conftest
        import lift_fragment_mca as _lfm
        
        original_cflags = ["-target", "aarch64-linux-gnu", "-O2"]
        _conftest.CFLAGS = list(original_cflags)
        _lfm.CFLAGS = list(original_cflags)
        
        captured_cflags = []
        
        def mock_run_pipeline(source, output_dir, **kwargs):
            # Capture what CFLAGS is at the time _run_pipeline executes
            captured_cflags.extend(_conftest.CFLAGS)
            raise RuntimeError("stop here")
        
        monkeypatch.setattr(tool, "_run_pipeline", mock_run_pipeline)
        
        # Create a source file
        source = tmp_dir / "test.c"
        source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
        
        entry = {
            "directory": str(tmp_dir),
            "file": str(source),
            "output": "test.o",
            "arguments": [
                "clang", "-target", "aarch64-linux-gnu", "-O2",
                "-I/usr/local/include", "-DDEBUG=1",
                "-c", str(source), "-o", "test.o",
            ],
        }
        args = MagicMock()
        args.arm_lifter = "/usr/bin/true"
        args.cc = None
        args.output_dir = tmp_dir / "output"
        args.output_dir.mkdir(parents=True, exist_ok=True)
        args.mcpu = "generic"
        args.mattr = ""
        args.mca_iterations = 100
        args.min_source_instructions = 2
        args.min_target_instructions = 2
        args.function = None
        args.write_md = False
        
        log_fh = open(tmp_dir / "log.txt", "w")
        tool.process_entry(entry, args, tmp_dir, log_fh, 0, 1)
        log_fh.close()
        
        # Verify entry flags were merged
        assert "-I/usr/local/include" in captured_cflags
        assert "-DDEBUG=1" in captured_cflags
        # Verify original CFLAGS are still there
        assert "-target" in captured_cflags
        assert "-O2" in captured_cflags
        
        # Verify CFLAGS are restored after processing
        assert _conftest.CFLAGS == original_cflags
        assert _lfm.CFLAGS == original_cflags


class TestIsCc1Entry:
    """Test is_cc1_entry."""
    
    def test_cc1_entry(self):
        """Test detecting -cc1 entry."""
        entry = {"arguments": ["clang", "-cc1", "-O2", "test.c"]}
        assert tool.is_cc1_entry(entry) is True
    
    def test_driver_entry(self):
        """Test detecting driver entry."""
        entry = {"arguments": ["clang", "-O2", "-c", "test.c", "-o", "test.o"]}
        assert tool.is_cc1_entry(entry) is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])