.PHONY: clean

ARM_LIFTER=../../build/Release/arm-lifter

# Use zig cc for cross-compiling elf on mac (ships with Linux sysroot)
CC=zig cc
# CC=clang
CFLAGS=-target aarch64-linux -fno-sanitize=all -O0

SRC=Maze_novarargs.c
# SRC=struct_test.c
# SRC=indirect_call.c

# Derive target name from SRC (strip .c extension)
TARGET=$(basename $(SRC))

lift: $(TARGET).bc $(TARGET).o
	$(ARM_LIFTER) --src-bc $(TARGET).bc $(TARGET).o -o output/$(TARGET).lifted.ll 2>&1 | tee output_$(TARGET).txt

# Recompile the lifted .ll file and link into executable
recompile-arm64-linux: lift
	@for ll in output/*.lifted.ll; do \
		if [ -f "$$ll" ]; then \
			out=$${ll%.ll}.o; \
			echo "Compiling $$ll -> $$out"; \
			$(CC) -target aarch64-linux-gnu -c $$ll -o $$out || echo "FAILED: $$ll"; \
		fi; \
	done
	$(CC) -target aarch64-linux-gnu output/*.lifted.o -o output/$(TARGET)_lifted_arm64_linux

recompile-x64-linux: lift
	@for ll in output/*.lifted.ll; do \
		if [ -f "$$ll" ]; then \
			out=$${ll%.ll}.o; \
			echo "Compiling $$ll -> $$out"; \
			$(CC) -target x86_64-linux-gnu -c $$ll -o $$out || echo "FAILED: $$ll"; \
		fi; \
	done
	$(CC) -target x86_64-linux-gnu output/*.lifted.o -o output/$(TARGET)_lifted_x64_linux

$(TARGET).bc: $(SRC)
	$(CC) $(CFLAGS) -emit-llvm -c $(SRC) -o $(TARGET).bc

$(TARGET).ll: $(SRC)
	$(CC) $(CFLAGS) -emit-llvm -S $(SRC) -o $(TARGET).ll

$(TARGET).o: $(SRC)
	$(CC) $(CFLAGS) -c $(SRC) -o $(TARGET).o
	
$(TARGET): $(SRC)
	$(CC) $(CFLAGS) $(SRC) -o $(TARGET)

clean:
	rm -f *.bc *.ll *.o *.txt
	rm -rf output
