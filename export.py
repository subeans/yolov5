# YOLOv5 🚀 by Ultralytics, GPL-3.0 license
"""
Export a YOLOv5 PyTorch model to other formats. TensorFlow exports authored by https://github.com/zldrobit
Format                      | `export.py --include`         | Model
---                         | ---                           | ---
PyTorch                     | -                             | yolov5s.pt
TensorRT                    | `engine`                      | yolov5s.engine
TensorFlow SavedModel       | `saved_model`                 | yolov5s_saved_model/
TensorFlow GraphDef         | `pb`                          | yolov5s.pb
Requirements:
    $ pip3 install torch==1.10.1+cu102 torchvision==0.11.2+cu102 torchaudio==0.10.1 -f https://download.pytorch.org/whl/torch_stable.htmlpip3 install opencv-python
  # GPU
Usage:
    $ python path/to/export.py --weights yolov5s.pt --include saved_model engine
Inference:
    $ python path/to/detect.py --weights yolov5s_saved_model        # TensorFlow SavedModel
                                         yolov5s.onnx               # ONNX Runtime or OpenCV DNN with --dnn
                                         yolov5s.engine             # TensorRT
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import warnings
from pathlib import Path

import pandas as pd
import torch
import yaml
from torch.utils.mobile_optimizer import optimize_for_mobile

FILE = Path(__file__).resolve()
ROOT = FILE.parents[0]  # YOLOv5 root directory
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))  # add ROOT to PATH
if platform.system() != 'Windows':
    ROOT = Path(os.path.relpath(ROOT, Path.cwd()))  # relative

from models.experimental import attempt_load
from models.yolo import Detect
from utils.dataloaders import LoadImages
from utils.general import (LOGGER, check_dataset, check_img_size, check_requirements, check_version, colorstr,
                           file_size, print_args, url2file)
from utils.torch_utils import select_device


def export_formats():
    # YOLOv5 export formats
    x = [
        ['PyTorch', '-', '.pt', True],
        ['TensorRT', 'engine', '.engine', True],
        ['TensorFlow SavedModel', 'saved_model', '_saved_model', True],
        ['TensorFlow GraphDef', 'pb', '.pb', True],
        ]
    return pd.DataFrame(x, columns=['Format', 'Argument', 'Suffix', 'GPU'])


def export_onnx(model, im, file, opset, train, dynamic, simplify, prefix=colorstr('ONNX:')):
    # YOLOv5 ONNX export
    try:
        check_requirements(('onnx',))
        import onnx

        LOGGER.info(f'\n{prefix} starting export with onnx {onnx.__version__}...')
        f = file.with_suffix('.onnx')

        torch.onnx.export(
            model,
            im,
            f,
            verbose=False,
            opset_version=opset,
            training=torch.onnx.TrainingMode.TRAINING if train else torch.onnx.TrainingMode.EVAL,
            do_constant_folding=not train,
            input_names=['images'],
            output_names=['output'],
            dynamic_axes={
                'images': {
                    0: 'batch',
                    2: 'height',
                    3: 'width'},  # shape(1,3,640,640)
                'output': {
                    0: 'batch',
                    1: 'anchors'}  # shape(1,25200,85)
            } if dynamic else None)

        # Checks
        model_onnx = onnx.load(f)  # load onnx model
        onnx.checker.check_model(model_onnx)  # check onnx model

        # Metadata
        d = {'stride': int(max(model.stride)), 'names': model.names}
        for k, v in d.items():
            meta = model_onnx.metadata_props.add()
            meta.key, meta.value = k, str(v)
        onnx.save(model_onnx, f)

        # Simplify
        if simplify:
            try:
                check_requirements(('onnx-simplifier',))
                import onnxsim

                LOGGER.info(f'{prefix} simplifying with onnx-simplifier {onnxsim.__version__}...')
                model_onnx, check = onnxsim.simplify(model_onnx,
                                                     dynamic_input_shape=dynamic,
                                                     input_shapes={'images': list(im.shape)} if dynamic else None)
                assert check, 'assert check failed'
                onnx.save(model_onnx, f)
            except Exception as e:
                LOGGER.info(f'{prefix} simplifier failure: {e}')
        LOGGER.info(f'{prefix} export success, saved as {f} ({file_size(f):.1f} MB)')
        return f
    except Exception as e:
        LOGGER.info(f'{prefix} export failure: {e}')


def export_engine(model, im, file, train, half, simplify, workspace=4, verbose=False, prefix=colorstr('TensorRT:')):
    # YOLOv5 TensorRT export https://developer.nvidia.com/tensorrt
    # torch -> onnx -> tensorRT 
    try:
        assert im.device.type != 'cpu', 'export running on CPU but must be on GPU, i.e. `python export.py --device 0`'
        try:
            import tensorrt as trt
        except Exception:
            if platform.system() == 'Linux':
                check_requirements(('nvidia-tensorrt',), cmds=('-U --index-url https://pypi.ngc.nvidia.com',))
            import tensorrt as trt

        if trt.__version__[0] == '7':  # TensorRT 7 handling https://github.com/ultralytics/yolov5/issues/6012
            grid = model.model[-1].anchor_grid
            model.model[-1].anchor_grid = [a[..., :1, :1, :] for a in grid]
            export_onnx(model, im, file, 12, train, False, simplify)  # opset 12
            model.model[-1].anchor_grid = grid
        else:  # TensorRT >= 8
            check_version(trt.__version__, '8.0.0', hard=True)  # require tensorrt>=8.0.0
            export_onnx(model, im, file, 13, train, False, simplify)  # opset 13
        onnx = file.with_suffix('.onnx')

        LOGGER.info(f'\n{prefix} starting export with TensorRT {trt.__version__}...')
        assert onnx.exists(), f'failed to export ONNX file: {onnx}'
        f = file.with_suffix('.engine')  # TensorRT engine file
        logger = trt.Logger(trt.Logger.INFO)
        if verbose:
            logger.min_severity = trt.Logger.Severity.VERBOSE

        builder = trt.Builder(logger)
        config = builder.create_builder_config()
        config.max_workspace_size = workspace * 1 << 30
        # config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, workspace << 30)  # fix TRT 8.4 deprecation notice

        flag = (1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH))
        network = builder.create_network(flag)
        parser = trt.OnnxParser(network, logger)
        if not parser.parse_from_file(str(onnx)):
            raise RuntimeError(f'failed to load ONNX file: {onnx}')

        inputs = [network.get_input(i) for i in range(network.num_inputs)]
        outputs = [network.get_output(i) for i in range(network.num_outputs)]
        LOGGER.info(f'{prefix} Network Description:')
        for inp in inputs:
            LOGGER.info(f'{prefix}\tinput "{inp.name}" with shape {inp.shape} and dtype {inp.dtype}')
        for out in outputs:
            LOGGER.info(f'{prefix}\toutput "{out.name}" with shape {out.shape} and dtype {out.dtype}')

        LOGGER.info(f'{prefix} building FP{16 if builder.platform_has_fast_fp16 and half else 32} engine in {f}')
        
        # default 로 FP32 로 engine 을 build 하며 만약 FP16으로 build 하고 싶은 경우 half 를 True 로 입력하면 됨
        if builder.platform_has_fast_fp16 and half:
            config.set_flag(trt.BuilderFlag.FP16)
        with builder.build_engine(network, config) as engine, open(f, 'wb') as t:
            t.write(engine.serialize())
        LOGGER.info(f'{prefix} export success, saved as {f} ({file_size(f):.1f} MB)')
        return f
    except Exception as e:
        LOGGER.info(f'\n{prefix} export failure: {e}')


def export_saved_model(model,
                       im,
                       file,
                       dynamic,
                       tf_nms=False,
                       agnostic_nms=False,
                       topk_per_class=100,
                       topk_all=100,
                       iou_thres=0.45,
                       conf_thres=0.25,
                       keras=False,
                       prefix=colorstr('TensorFlow SavedModel:')):
    # YOLOv5 TensorFlow SavedModel export
    try:
        import tensorflow as tf
        from tensorflow.python.framework.convert_to_constants import convert_variables_to_constants_v2

        from models.tf import TFDetect, TFModel

        LOGGER.info(f'\n{prefix} starting export with tensorflow {tf.__version__}...')
        f = str(file).replace('.pt', '_saved_model')
        batch_size, ch, *imgsz = list(im.shape)  # BCHW

        # export 시 imgsz 가 입력파라미터로 입려되는데 imgsize 에 batchsize 정보가 있음 
        tf_model = TFModel(cfg=model.yaml, model=model, nc=model.nc, imgsz=imgsz)

        im = tf.zeros((batch_size, *imgsz, ch))  # BHWC order for TensorFlow
        _ = tf_model.predict(im, tf_nms, agnostic_nms, topk_per_class, topk_all, iou_thres, conf_thres)

        inputs = tf.keras.Input(shape=(*imgsz, ch), batch_size=None if dynamic else batch_size)
        outputs = tf_model.predict(inputs, tf_nms, agnostic_nms, topk_per_class, topk_all, iou_thres, conf_thres)
        keras_model = tf.keras.Model(inputs=inputs, outputs=outputs)
        keras_model.trainable = False
        keras_model.summary()
        if keras:
            keras_model.save(f, save_format='tf')
        else:
            spec = tf.TensorSpec(keras_model.inputs[0].shape, keras_model.inputs[0].dtype)
            m = tf.function(lambda x: keras_model(x))  # full model
            m = m.get_concrete_function(spec)
            frozen_func = convert_variables_to_constants_v2(m)
            tfm = tf.Module()
            tfm.__call__ = tf.function(lambda x: frozen_func(x)[:4] if tf_nms else frozen_func(x)[0], [spec])
            tfm.__call__(im)
            tf.saved_model.save(tfm,
                                f,
                                options=tf.saved_model.SaveOptions(experimental_custom_gradients=False)
                                if check_version(tf.__version__, '2.6') else tf.saved_model.SaveOptions())
        LOGGER.info(f'{prefix} export success, saved as {f} ({file_size(f):.1f} MB)')
        return keras_model, f
    except Exception as e:
        LOGGER.info(f'\n{prefix} export failure: {e}')
        return None, None


def export_pb(keras_model, file, prefix=colorstr('TensorFlow GraphDef:')):
    # YOLOv5 TensorFlow GraphDef *.pb export https://github.com/leimao/Frozen_Graph_TensorFlow
    try:
        import tensorflow as tf
        from tensorflow.python.framework.convert_to_constants import convert_variables_to_constants_v2

        LOGGER.info(f'\n{prefix} starting export with tensorflow {tf.__version__}...')
        f = file.with_suffix('.pb')

        m = tf.function(lambda x: keras_model(x))  # full model
        m = m.get_concrete_function(tf.TensorSpec(keras_model.inputs[0].shape, keras_model.inputs[0].dtype))
        frozen_func = convert_variables_to_constants_v2(m)
        frozen_func.graph.as_graph_def()
        tf.io.write_graph(graph_or_graph_def=frozen_func.graph, logdir=str(f.parent), name=f.name, as_text=False)

        LOGGER.info(f'{prefix} export success, saved as {f} ({file_size(f):.1f} MB)')
        return f
    except Exception as e:
        LOGGER.info(f'\n{prefix} export failure: {e}')



@torch.no_grad()
def run(
        data=ROOT / 'data/coco128.yaml',  # 'dataset.yaml path'
        weights=ROOT / 'yolov5s.pt',  # weights path
        imgsz=(640, 640),  # image (height, width)
        batch_size=1,  # batch size
        device='cpu',  # cuda device, i.e. 0 or 0,1,2,3 or cpu
        include=('torchscript', 'onnx'),  # include formats
        half=False,  # FP16 half-precision export
        inplace=False,  # set YOLOv5 Detect() inplace=True
        train=False,  # model.train() mode
        keras=False,  # use Keras
        optimize=False,  # TorchScript: optimize for mobile
        int8=False,  # CoreML/TF INT8 quantization
        dynamic=False,  # ONNX/TF: dynamic axes
        simplify=False,  # ONNX: simplify model
        opset=12,  # ONNX: opset version
        verbose=False,  # TensorRT: verbose log
        workspace=4,  # TensorRT: workspace size (GB)
        nms=False,  # TF: add NMS to model
        agnostic_nms=False,  # TF: add agnostic NMS to model
        topk_per_class=100,  # TF.js NMS: topk per class to keep
        topk_all=100,  # TF.js NMS: topk for all classes to keep
        iou_thres=0.45,  # TF.js NMS: IoU threshold
        conf_thres=0.25,  # TF.js NMS: confidence threshold
):
    t = time.time()
    include = [x.lower() for x in include]  # to lowercase
    fmts = tuple(export_formats()['Argument'][1:])  # --include arguments
    flags = [x in include for x in fmts]
    print(flags)
    assert sum(flags) == len(include), f'ERROR: Invalid --include {include}, valid --include arguments are {fmts}'
    engine, saved_model, pb = flags  # export booleans
    file = Path(url2file(weights) if str(weights).startswith(('http:/', 'https:/')) else weights)  # PyTorch weights

    # Load PyTorch model
    device = select_device(device)
    if half:
        assert device.type != 'cpu' or coreml or xml, '--half only compatible with GPU export, i.e. use --device 0'
        assert not dynamic, '--half not compatible with --dynamic, i.e. use either --half or --dynamic but not both'
    
    # load torch model - torch version
    model = attempt_load(weights, device=device, inplace=True, fuse=True)  # load FP32 model
    nc, names = model.nc, model.names  # number of classes, class names

    # Checks
    imgsz *= 2 if len(imgsz) == 1 else 1  # expand
    assert nc == len(names), f'Model class count {nc} != len(names) {len(names)}'

    # Input
    gs = int(max(model.stride))  # grid size (max stride)
    imgsz = [check_img_size(x, gs) for x in imgsz]  # verify img_size are gs-multiples
    im = torch.zeros(batch_size, 3, *imgsz).to(device)  # image size(1,3,320,192) BCHW iDetection

    # Update model
    if half :
        im, model = im.half(), model.half()  # to FP16
    model.train() if train else model.eval()  # training mode = no Detect() layer grid construction
    for k, m in model.named_modules():
        if isinstance(m, Detect):
            m.inplace = inplace
            m.onnx_dynamic = dynamic
            m.export = True

    for _ in range(2):
        y = model(im)  # dry runs
    shape = tuple(y[0].shape)  # model output shape
    LOGGER.info(f"\n{colorstr('PyTorch:')} starting from {file} with output shape {shape} ({file_size(file):.1f} MB)")

    # Exports
    f = [''] * 10  # exported filenames
    warnings.filterwarnings(action='ignore', category=torch.jit.TracerWarning)  # suppress TracerWarning
    if engine:  # TensorRT required before ONNX
        f[1] = export_engine(model, im, file, train, half, simplify, workspace, verbose)

    # TensorFlow Exports
    if any((saved_model, pb)):
        if int8 :  # TFLite --int8 bug https://github.com/ultralytics/yolov5/issues/5707
            check_requirements(('flatbuffers==1.12',))  # required before `import tensorflow`
        model, f[5] = export_saved_model(model.cpu(),
                                         im,
                                         file,
                                         dynamic,
                                         tf_nms=nms or agnostic_nms or tfjs,
                                         agnostic_nms=agnostic_nms or tfjs,
                                         topk_per_class=topk_per_class,
                                         topk_all=topk_all,
                                         iou_thres=iou_thres,
                                         conf_thres=conf_thres,
                                         keras=keras)
        if pb :  # pb prerequisite to tfjs
            f[6] = export_pb(model, file)

    # Finish
    f = [str(x) for x in f if x]  # filter out '' and None
    if any(f):
        LOGGER.info(f'\nExport complete ({time.time() - t:.2f}s)'
                    f"\nResults saved to {colorstr('bold', file.parent.resolve())}"
                    f"\nDetect:          python detect.py --weights {f[-1]}"
                    f"\nPyTorch Hub:     model = torch.hub.load('ultralytics/yolov5', 'custom', '{f[-1]}')"
                    f"\nValidate:        python val.py --weights {f[-1]}"
                    f"\nVisualize:       https://netron.app")
    return f  # return list of exported files/dirs


def parse_opt():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=str, default=ROOT / 'data/coco128.yaml', help='dataset.yaml path')
    parser.add_argument('--weights', nargs='+', type=str, default=ROOT / 'yolov5s.pt', help='model.pt path(s)')
    parser.add_argument('--imgsz', '--img', '--img-size', nargs='+', type=int, default=[640, 640], help='image (h, w)')
    # batchsize default 1
    parser.add_argument('--batch-size', type=int, default=1, help='batch size')
    parser.add_argument('--device', default='cpu', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--half', action='store_true', help='FP16 half-precision export')
    parser.add_argument('--inplace', action='store_true', help='set YOLOv5 Detect() inplace=True')
    parser.add_argument('--train', action='store_true', help='model.train() mode')
    parser.add_argument('--keras', action='store_true', help='TF: use Keras')
    parser.add_argument('--optimize', action='store_true', help='TorchScript: optimize for mobile')
    parser.add_argument('--int8', action='store_true', help='CoreML/TF INT8 quantization')
    parser.add_argument('--dynamic', action='store_true', help='ONNX/TF: dynamic axes')
    parser.add_argument('--simplify', action='store_true', help='ONNX: simplify model')
    parser.add_argument('--opset', type=int, default=12, help='ONNX: opset version')
    parser.add_argument('--verbose', action='store_true', help='TensorRT: verbose log')
    parser.add_argument('--workspace', type=int, default=4, help='TensorRT: workspace size (GB)')
    parser.add_argument('--nms', action='store_true', help='TF: add NMS to model')
    parser.add_argument('--agnostic-nms', action='store_true', help='TF: add agnostic NMS to model')
    parser.add_argument('--topk-per-class', type=int, default=100, help='TF.js NMS: topk per class to keep')
    parser.add_argument('--topk-all', type=int, default=100, help='TF.js NMS: topk for all classes to keep')
    parser.add_argument('--iou-thres', type=float, default=0.45, help='TF.js NMS: IoU threshold')
    parser.add_argument('--conf-thres', type=float, default=0.25, help='TF.js NMS: confidence threshold')
    parser.add_argument('--include',
                        nargs='+',
                        default=['saved_model'],
                        help='torchscript, onnx, openvino, engine, coreml, saved_model, pb, tflite, edgetpu, tfjs')
    opt = parser.parse_args()
    print_args(vars(opt))
    return opt


def main(opt):
    for opt.weights in (opt.weights if isinstance(opt.weights, list) else [opt.weights]):
        run(**vars(opt))


if __name__ == "__main__":
    opt = parse_opt()
    main(opt)
