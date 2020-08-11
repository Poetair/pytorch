import torch
import torch.nn as nn
from torch.quantization.fake_quantize import FakeQuantize
from torch.quantization.observer import HistogramObserver
from torch.quantization.qconfig import *
from torch.quantization.fake_quantize import *
import torch.nn.qat.modules as nnqat
from torch.quantization.default_mappings import DEFAULT_QAT_MODULE_MAPPING
from torch.quantization import QuantStub, DeQuantStub
import copy
_supported_modules = {nn.Conv2d, nn.Linear}

def clipped_sigmoid(continous_V):
    sigmoid_applied = torch.sigmoid(continous_V * 100)
    scale_n_add = (sigmoid_applied * 1.2) - 0.1
    clip = torch.clamp(scale_n_add, 0, 1)
    return clip

def modified_quantized(model, x):
    weight = x
    continous_V = model.continous_V
    scale = model.scale

    W_over_S = torch.div(weight, scale)
    W_over_S = torch.floor(W_over_S)
    W_plus_H = W_over_S + clipped_sigmoid(continous_V)

    rtn = scale * torch.clamp(W_plus_H, model.quant_min, model.quant_max)
    return rtn

def loss_function_leaf(model, count):
    high = 8
    low = 2
    beta = count / 10 * (high - low) + low
    _lambda = 1

    adaround_instance = model.wrapped_module.weight_fake_quant
    float_weight = model.wrapped_module.weight

    clipped_weight = modified_quantized(adaround_instance, float_weight)
    quantized_weight = torch.fake_quantize_per_tensor_affine(clipped_weight, float(adaround_instance.scale),
                                                             int(adaround_instance.zero_point), adaround_instance.quant_min,
                                                             adaround_instance.quant_max)
    Frobenius_norm = torch.norm(float_weight - quantized_weight)
    # Frobenius_norm = torch.norm(model.float_output - model.quantized_output)
    # bring back x in expression

    scale = adaround_instance.scale
    continous_V = adaround_instance.continous_V

    clip_V = clipped_sigmoid(continous_V)
    spreading_range = torch.abs((2 * clip_V) - 1)
    one_minus_beta = 1 - (spreading_range ** beta)  # torch.exp
    regulization = torch.sum(one_minus_beta)

    print("loss function break down: ", Frobenius_norm * 100, _lambda * regulization)
    print("sqnr of float and quantized: ", computeSqnr(float_weight, quantized_weight))
    return Frobenius_norm * 100 + _lambda * regulization

def loss_function(model, count, white_list=(nnqat.Conv2d,)):
    result = torch.Tensor([0])
    for name, submodule in model.named_modules():
        if isinstance(submodule, OuputWrapper):
            result = result + loss_function_leaf(submodule, count)
    return result

def computeSqnr(x, y):
    Ps = torch.norm(x)
    Pn = torch.norm(x - y)
    return 20 * torch.log10(Ps / Pn)

def get_module(model, name):
    ''' Given name of submodule, this function grabs the submodule from given model
    '''
    curr = model
    name = name.split('.')
    for subname in name:
        if subname == '':
            return curr
        curr = curr._modules[subname]
    return curr

def get_parent_module(model, name):
    ''' Given name of submodule, this function grabs the parent of the submodule, from given model
    '''
    curr = model
    name = name.split('.')[:-1]
    for subname in name:
        if subname == '':
            return curr
        curr = curr._modules[subname]
    return curr

class adaround(FakeQuantize):
    def __init__(self, *args, **keywords):
        super(adaround, self).__init__(*args, **keywords)
        self.continous_V = None
        self.tuning = False

    def forward(self, X):
        if self.observer_enabled[0] == 1:
            self.activation_post_process(X.detach())
            _scale, _zero_point = self.calculate_qparams()
            _scale, _zero_point = _scale.to(self.scale.device), _zero_point.to(self.zero_point.device)
            self.scale = _scale
            self.zero_point = _zero_point

        if self.tuning:
            assert X is not None
            X = modified_quantized(self, X)

        if self.fake_quant_enabled[0] == 1:
            if self.qscheme == torch.per_channel_symmetric or self.qscheme == torch.per_channel_affine:
                X = torch.fake_quantize_per_channel_affine(X, self.scale, self.zero_point,
                                                           self.ch_axis, self.quant_min, self.quant_max)
            else:
                X = torch.fake_quantize_per_tensor_affine(X, float(self.scale),
                                                          int(self.zero_point), self.quant_min,
                                                          self.quant_max)
        return X


class ConvChain(nn.Module):
    def __init__(self):
        super(ConvChain, self).__init__()
        self.conv2d1 = nn.Conv2d(3, 4, 5, 5)
        self.conv2d2 = nn.Conv2d(4, 5, 5, 5)
        self.conv2d3 = nn.Conv2d(5, 6, 5, 5)

    def forward(self, x):
        x1 = self.conv2d1(x)
        x2 = self.conv2d2(x1)
        x3 = self.conv2d3(x2)
        return x3

class OuputWrapper(nn.Module):
    def __init__(self, model):
        super(OuputWrapper, self).__init__()
        self.wrapped_module = model
        self.float_output = None
        self.quantized_output = None
        self.on = False
        self.quant = QuantStub()
        self.dequant = DeQuantStub()

    def forward(self, x):
        x = self.quant(x)
        if self.on:
            self.wrapped_module.activation_post_process.disable_fake_quant()
            self.wrapped_module.weight_fake_quant.disable_fake_quant()
            self.float_output = self.wrapped_module(x).detach()

            self.wrapped_module.activation_post_process.enable_fake_quant()
            self.wrapped_module.weight_fake_quant.enable_fake_quant()
            self.quantized_output = self.wrapped_module(x)

            return self.dequant(self.quantized_output)
        else:
            return self.dequant(self.wrapped_module(x))


araround_fake_quant = adaround.with_args(observer=HistogramObserver, quant_min=-128, quant_max=127,
                                         dtype=torch.qint8, qscheme=torch.per_tensor_symmetric, reduce_range=False)

adaround_qconfig = QConfig(activation=default_fake_quant,
                           weight=araround_fake_quant)

def add_wrapper_class(model, white_list=DEFAULT_QAT_MODULE_MAPPING.keys()):
    for name, submodule in model.named_modules():
        print(type(submodule))
        if type(submodule) in white_list:
            parent = get_parent_module(model, name)
            submodule_name = name.split('.')[-1]
            parent._modules[submodule_name] = OuputWrapper(submodule)

def load_conv():
    model = ConvChain()
    copy_of_model = copy.deepcopy(model)
    model.train()
    img_data = [(torch.rand(10, 3, 125, 125, dtype=torch.float, requires_grad=True), torch.randint(0, 1, (2,), dtype=torch.long))
                for _ in range(500)]
    return model, img_data

def quick_function(qat_model, dummy, data_loader_test):
    # turning off observer and turning on tuning
    for name, submodule in qat_model.named_modules():
        if type(submodule) in _supported_modules:
            submodule.weight_fake_quant.disable_observer()
            # submodule.weight_fake_quant.enable_fake_quant()
            submodule.weight_fake_quant.tuning = True


    V_s = add_wrapper_class(qat_model)

    def uniform_images():
        for image, target in data_loader_test:
            yield image
    generator = uniform_images()

    batch = 0
    for name, submodule in qat_model.named_modules():
        if isinstance(submodule, OuputWrapper):
            submodule.wrapped_module.weight_fake_quant.enable_observer()
            if batch < 3:
                def dummy_generator():
                    yield submodule.wrapped_module.weight_fake_quant.continous_V
                optimizer = torch.optim.Adam(dummy_generator(), lr=.1)

                for count in range(10):
                    output = qat_model(next(generator))
                    # loss = loss_function(qat_model, count)
                    loss = loss_function_leaf(submodule, count)

                    print("loss: ", loss)
                    optimizer.zero_grad()
                    loss.backward()
                    optimizer.step()
                    try:
                        print(submodule.wrapped_module.weight_fake_quant.continous_V[0][0][:][:])
                    except IndexError:
                        print("ruh roh")

                    print("running count during optimazation: ", count)
            if batch == 3:
                return qat_model
            batch += 1
            submodule.wrapped_module.weight_fake_quant.disable_observer()


    # qat_model.eval()
    # torch.quantization.convert(qat_model, inplace=True)
    return qat_model

def learn_adaround(float_model, data_loader_test):
    # generator to get uniform distribution of images, i.e. not always taking the front 300
    def uniform_images():
        while True:
            for image, target in data_loader_test:
                yield image
    generator = uniform_images()



    def optimize_V(leaf_module):
        '''Takes in a leaf module with an adaround attached to its
        weight_fake_quant attribute'''
        def dummy_generator():
            yield leaf_module.wrapped_module.weight_fake_quant.continous_V
        optimizer = torch.optim.Adam(dummy_generator(), lr=10)

        for count in range(10):
            output = float_model(next(generator))
            # loss = loss_function(qat_model, count)
            loss = loss_function_leaf(leaf_module, count)

            print("loss: ", loss)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            try:
                print(leaf_module.wrapped_module.weight_fake_quant.continous_V[0][0][:][:])
            except IndexError:
                print("ruh roh")
            print("running count during optimazation: ", count)

    V_s = add_wrapper_class(float_model, _supported_modules)

    batch = 0
    for name, submodule in float_model.named_modules():
        if isinstance(submodule, OuputWrapper):
            batch += 1
            if batch <= 1:

                submodule.on = True
                print("training submodule")
                submodule.wrapped_module.qconfig = adaround_qconfig
                torch.quantization.prepare_qat(submodule, inplace=True)
                for count in range(100):
                    float_model(next(generator))
                submodule.wrapped_module.weight_fake_quant.disable_observer()

                # try randomizing values for contin V
                submodule.wrapped_module.weight_fake_quant.continous_V = \
                    torch.nn.Parameter(torch.ones(submodule.wrapped_module.weight.size()) / 10)

                print("quantized submodule")
                optimize_V(submodule)
                print("finished optimizing adaround instance")
                torch.quantization.convert(submodule, inplace=True)
                submodule.on = False
            if batch == 1:
                torch.quantization.convert(float_model, inplace=True)
                return float_model

    torch.quantization.convert(float_model, inplace=True)
    return float_model

if __name__ == "__main__":
    # main()
    learn_adaround(*load_conv())
