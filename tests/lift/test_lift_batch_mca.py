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
