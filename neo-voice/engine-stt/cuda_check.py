"""Diagnostico de carga das DLLs CUDA para o onnxruntime CUDA EP (Windows)."""
import os
import sys
import glob
import site
import ctypes


def nvidia_bins():
    roots = list(site.getsitepackages()) + [os.path.join(sys.prefix, "Lib", "site-packages")]
    dirs = []
    for sp in dict.fromkeys(roots):
        for b in glob.glob(os.path.join(sp, "nvidia", "*", "bin")):
            if os.path.isdir(b):
                dirs.append(os.path.normpath(b))
    return list(dict.fromkeys(dirs))


bins = nvidia_bins()
print("== nvidia bins:")
for b in bins:
    print("  ", b, "->", len(glob.glob(os.path.join(b, "*.dll"))), "dlls")

print("== conteudo cublas:")
for b in bins:
    if "cublas" in b.lower():
        for f in sorted(glob.glob(os.path.join(b, "*.dll"))):
            print("    ", os.path.basename(f))

# fix: PATH + add_dll_directory
os.environ["PATH"] = os.pathsep.join(bins) + os.pathsep + os.environ.get("PATH", "")
for b in bins:
    try:
        os.add_dll_directory(b)
    except Exception as e:
        print("add_dll fail", b, e)


def tryload(name):
    try:
        ctypes.WinDLL(name)
        print("  LOAD OK  :", name)
    except OSError as e:
        print("  LOAD FAIL:", name, "->", str(e)[:120])


print("== ctypes WinDLL:")
for n in ["vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll",
          "cudart64_12.dll", "cublas64_12.dll", "cublasLt64_12.dll",
          "cufft64_11.dll", "cudnn64_9.dll", "nvrtc64_120_0.dll"]:
    tryload(n)

print("== onnxruntime CUDA session test:")
import numpy as np
import onnxruntime as ort
print("  ort", ort.__version__, ort.get_available_providers())
try:
    import onnx
    from onnx import helper, TensorProto
    X = helper.make_tensor_value_info("X", TensorProto.FLOAT, [1, 4])
    Y = helper.make_tensor_value_info("Y", TensorProto.FLOAT, [1, 4])
    node = helper.make_node("Relu", ["X"], ["Y"])
    g = helper.make_graph([node], "g", [X], [Y])
    m = helper.make_model(g, opset_imports=[helper.make_opsetid("", 13)])
    path = os.path.join(os.path.dirname(__file__), "tiny.onnx")
    onnx.save(m, path)
    s = ort.InferenceSession(path, providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
    print("  session providers:", s.get_providers())
    out = s.run(None, {"X": np.array([[-1, 2, -3, 4]], dtype=np.float32)})
    print("  run ok:", out[0].tolist())
except Exception as e:
    print("  onnx test skip/fail:", repr(e)[:200])
