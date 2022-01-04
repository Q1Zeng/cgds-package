import torch
import torch.autograd as autograd
import warnings


def vectorize_grad(params):
    '''
    Extract .grad field of parameters and concatenate them into a vector.
    If grad is None, it is replace with zeros.
    :param params: list of parameters
    :return: vector
    '''
    grad_list = []
    for p in params:
        if p.grad is not None:
            grad_list.append(p.grad.contiguous().view(-1))
            del p.grad
        else:
            # replace None with zeros
            grad_list.append(torch.zeros_like(p).view(-1))
    return torch.cat(grad_list)


def conjugate_gradient(grad_x, grad_y,
                       x_params, y_params,
                       b, x=None, nsteps=None,
                       tol=1e-10, atol=1e-16,
                       lr_x=1.0, lr_y=1.0,
                       device=torch.device('cpu')):
    """
    :param grad_x:
    :param grad_y:
    :param x_params:
    :param y_params:
    :param b: vec
    :param nsteps: max number of steps
    :param residual_tol:
    :return: A ** -1 * b
    h_1 = D_yx * p
    h_2 = D_xy * D_yx * p
    A = I + lr_x * D_xy * lr_y * D_yx
    """
    if nsteps is None:
        nsteps = b.shape[0]
    if x is None:
        x = torch.zeros(b.shape[0], device=b.device)
        r = b.clone().detach()
    else:
        h1 = Hvp_vec(grad_vec=grad_x, params=y_params, vec=x, retain_graph=True).detach_().mul(lr_y)
        h2 = Hvp_vec(grad_vec=grad_y, params=x_params, vec=h1, retain_graph=True).detach_().mul(lr_x)
        Avx = x + h2
        r = b.clone().detach() - Avx
        nsteps -= 1

    p = r.clone().detach()
    rdotr = torch.dot(r, r)
    residual_tol = tol * torch.dot(b, b)
    if rdotr < residual_tol or rdotr < atol:
        return x, 1


    for i in range(nsteps):
        # To compute Avp
        h_1 = Hvp_vec(grad_vec=grad_x, params=y_params, vec=p, retain_graph=True).detach_().mul(lr_y)
        h_2 = Hvp_vec(grad_vec=grad_y, params=x_params, vec=h_1, retain_graph=True).detach_().mul(lr_x)
        Avp_ = p + h_2

        alpha = rdotr / torch.dot(p, Avp_)
        x.data.add_(alpha * p)

        r.data.add_(- alpha * Avp_)
        new_rdotr = torch.dot(r, r)
        beta = new_rdotr / rdotr
        p = r + beta * p
        rdotr = new_rdotr
        if rdotr < residual_tol or rdotr < atol:
            break
    if i > 99:
        warnings.warn('CG iter num: %d' % (i + 1))
    return x, i + 1


def Hvp_vec(grad_vec, params, vec,
            backward=False,
            retain_graph=False,
            trigger=None,
            reducer=None,
            rebuild=False):
    '''
    Parameters:
        - grad_vec: Tensor of which the Hessian vector product will be computed
        - params: list of params, w.r.t which the Hessian will be computed
        - vec: The "vector" in Hessian vector product
        - retain_graph: keep the computation graph
        - trigger: scalar that will be used to trigger gradient reduction. Only needed when using DDP.
        - reducer: DDP reducer of the corresponding parameter
        - rebuild: If True, check last backward is reduced and rebuild bucket
    return: Hessian vector product
    '''
    if torch.isnan(grad_vec).any():
        raise ValueError('Gradvec nan')
    if torch.isnan(vec).any():
        raise ValueError('vector nan')
        # zero padding for None
    if backward:
        zero_grad(params)
        if reducer is not None:
            if rebuild:
                reducer._rebuild_buckets()
            reducer.prepare_for_backward([])
        autograd.backward(grad_vec + 0.0 * trigger, grad_tensors=vec,
                          inputs=params,
                          retain_graph=retain_graph)
        hvp = vectorize_grad(params)
    else:
        grad_grad = autograd.grad(grad_vec, params, grad_outputs=vec,
                                  retain_graph=retain_graph, create_graph=False,
                                  allow_unused=True)
        grad_list = []
        for i, p in enumerate(params):
            if grad_grad[i] is None:
                grad_list.append(torch.zeros_like(p).view(-1))
            else:
                grad_list.append(grad_grad[i].contiguous().view(-1))
        hvp = torch.cat(grad_list)
    if torch.isnan(hvp).any():
        raise ValueError('hvp Nan')
    return hvp


def general_conjugate_gradient(grad_x, grad_y,
                               x_params, y_params,
                               trigger,
                               b,
                               lr_x, lr_y,
                               x_reducer=None,
                               y_reducer=None,
                               rebuild=False,
                               backward=False,
                               x=None, nsteps=None,
                               tol=1e-10, atol=1e-16):
    '''
    Conjugate gradient algorithm for adaptive competitive gradient descent
    :param grad_x: grad w.r.t. x_params
    :param grad_y: grad w.r.t. y_params
    :param x_params: list of x parameters
    :param y_params: list of y parameters
    :param trigger: scalar, dummy loss term to trigger DDP comm hook
    :param b: the vector b in the linear system Ax=b
    :param lr_x: learning rate vector for x parameters
    :param lr_y: learning rate vector for y parameters
    :param x_reducer: reducer manager for x DDP module
    :param y_reducer: reducer manager for y DDP module
    :param rebuild: boolean, if True rebuild parameters for DDP. Only need for the first iteration.
    :param backward: True or False. If True, use backward to accumulate gradient.
                    Required to be True if working with DDP.
    :param x: initial guess of the solution
    :param nsteps: the maximum step of the CG inner loop
    :param tol: relative w.r.t. |b|^2 tolerance of the residual
    :param atol: absolute tolerance of the residual
    :return: (I + sqrt(lr_x) * D_xy * lr_y * D_yx * sqrt(lr_x)) ** -1 * b
    '''
    lr_x = lr_x.sqrt()
    if nsteps is None:
        nsteps = b.shape[0]
    if x is None:
        x = torch.zeros(b.shape[0], device=b.device)
        r = b.clone()
    else:
        h1 = Hvp_vec(grad_vec=grad_x, params=y_params,
                     vec=lr_x * x, backward=backward,
                     retain_graph=True,
                     trigger=trigger, reducer=y_reducer,
                     rebuild=rebuild).mul_(lr_y)
        h2 = Hvp_vec(grad_vec=grad_y, params=x_params,
                     vec=h1, backward=backward,
                     retain_graph=True,
                     trigger=trigger, reducer=x_reducer,
                     rebuild=rebuild).mul_(lr_x)
        Avx = x + h2
        r = b.clone() - Avx
        nsteps -= 1

    if grad_x.shape != b.shape:
        raise RuntimeError('CG: hessian vector product shape mismatch')
        
    # #calculate eigenvalues
    # x0 = torch.ones_like(x) #initial guess
    # D_yx = Hvp_vec(grad_vec=grad_x, params=y_params, vec=x0, retain_graph=True).detach_() #D_yx * x0
    # D_yx = D_yx / D_yx.max()
    # # print(f"D_yx: {D_yx}")

    # D_xy_D_yx = Hvp_vec(grad_vec=grad_y, params=x_params, vec=D_yx, retain_graph=True).detach_() #D_xy * D_yx * x0
    # D_xy_D_yx = D_xy_D_yx / D_xy_D_yx.max()
    # # print(f"D_xy_D_yx: {D_xy_D_yx}")

    # temp = D_xy_D_yx
    # print("starting calculating eigenValues")
    # for i in range(50):
    #   tempD_yx = Hvp_vec(grad_vec=grad_x, params=y_params, vec=temp, retain_graph=True).detach_() #D_yx * (D_xy*D_yx*x0)
    #   tempNew = Hvp_vec(grad_vec=grad_y, params=x_params, vec=tempD_yx, retain_graph=True).detach_() #D_xy * D_yx * (D_xy*D_yx*x0), target

    #   # print(f"tempNew.shape: {tempNew.shape}")
    #   normalizedTempNew = tempNew / tempNew.max() #normalize
    #   normalizedTempOld = temp / temp.max()
    #   # print(f"normalizedTempNew: {normalizedTempNew}")
    #   # print(f"normalizedTempOld: {normalizedTempOld}")

    #   print("unscaled tempNew: ", tempNew)
      
    #   offBy = abs(torch.norm(normalizedTempNew - normalizedTempOld, 2) / torch.norm(normalizedTempOld, 2))
    #   # print(f"at {i}, the relative norm is off by {offBy}")
    #   if offBy < 0.0001:
    #     # print(tempNew)
    #     # print(temp)
    #     D_yxEig = Hvp_vec(grad_vec=grad_x, params=y_params, vec=normalizedTempNew, retain_graph=True).detach_() # D_yx * eigenVector
    #     Ax = Hvp_vec(grad_vec=grad_y, params=x_params, vec=D_yxEig, retain_graph=True).detach_() #D_xy * D_yx * eigenVector
    #     eigenVal = Ax.dot(normalizedTempNew) / normalizedTempNew.dot(normalizedTempNew)

    #     print(f"at {i}, we found the (normalized) eigenvector {normalizedTempNew}, the eigenvalue is {eigenVal}. Ax: {Ax}, calculated as: {normalizedTempNew * eigenVal}")
    #     break
    #   temp = tempNew
    #   if i == 49:
    #     print(f"power method did not converge at i = {i}")

    p = r.clone().detach()
    rdotr = torch.dot(r, r)
    residual_tol = tol * torch.dot(b, b)
    if rdotr < residual_tol or rdotr < atol:
        return x, 1
    for i in range(nsteps):
        h_1 = Hvp_vec(grad_vec=grad_x, params=y_params,
                      vec=lr_x * p, backward=backward,
                      retain_graph=True,
                      trigger=trigger, reducer=y_reducer,
                      rebuild=rebuild).mul_(lr_y)
        h_2 = Hvp_vec(grad_vec=grad_y, params=x_params,
                      vec=h_1, backward=backward,
                      retain_graph=True,
                      trigger=trigger, reducer=x_reducer,
                      rebuild=rebuild).mul_(lr_x)
        Avp_ = p + h_2

        alpha = rdotr / torch.dot(p, Avp_)
        x.data.add_(alpha * p)
        r.data.add_(- alpha * Avp_)
        new_rdotr = torch.dot(r, r)
        beta = new_rdotr / rdotr
        rdotr = new_rdotr
        if rdotr < residual_tol or rdotr < atol:
            break
        p = r + beta * p
    if i > 100:
        warnings.warn('CG iter num: %d' % (i + 1))
#         torch.set_default_dtype(torch.double)

#         # calculate eigenvalues
#         x0 = torch.ones_like(x) #initial guess
#         D_yx = Hvp_vec(grad_vec=grad_x, params=y_params, vec=x0, retain_graph=True).detach_() #D_yx * x0
#         D_yx = D_yx / D_yx.max()
#         print(f"D_yx: {D_yx}")

#         D_xy_D_yx = Hvp_vec(grad_vec=grad_y, params=x_params, vec=D_yx, retain_graph=True).detach_() #D_xy * D_yx * x0
#         D_xy_D_yx = D_xy_D_yx / D_xy_D_yx.max()
#         print(f"D_xy_D_yx: {D_xy_D_yx}")

#         temp = D_xy_D_yx
#         print(f"iter_num = {i}, starting calculating eigenValues")
#         max_iter_pow = 500
#         for n in range(max_iter_pow):
#           tempD_yx = Hvp_vec(grad_vec=grad_x, params=y_params, vec=temp, retain_graph=True).detach_() #D_yx * (D_xy*D_yx*x0)
#           tempNew = Hvp_vec(grad_vec=grad_y, params=x_params, vec=tempD_yx, retain_graph=True).detach_() #D_xy * D_yx * (D_xy*D_yx*x0), target

#           # print(f"tempNew.shape: {tempNew.shape}")
#           normalizedTempNew = tempNew / tempNew.max() #normalize
#           normalizedTempOld = temp / temp.max()
#           # print(f"normalizedTempNew: {normalizedTempNew}")
#           # print(f"normalizedTempOld: {normalizedTempOld}")

#           print("unscaled tempNew: ", tempNew)
          
#           offBy = abs(torch.norm(normalizedTempNew - normalizedTempOld, 2) / torch.norm(normalizedTempOld, 2))
#           # print(f"at {i}, the relative norm is off by {offBy}")
#           if offBy < 0.001:
#             # print(tempNew)
#             # print(temp)
#             D_yxEig = Hvp_vec(grad_vec=grad_x, params=y_params, vec=normalizedTempNew, retain_graph=True).detach_() # D_yx * eigenVector
#             Ax = Hvp_vec(grad_vec=grad_y, params=x_params, vec=D_yxEig, retain_graph=True).detach_() #D_xy * D_yx * eigenVector
#             eigenVal = Ax.dot(normalizedTempNew) / normalizedTempNew.dot(normalizedTempNew)

#             print(f"at {n}'th power iteration, we found the (normalized) eigenvector {normalizedTempNew}, the eigenvalue is {eigenVal}. Ax: {Ax}, calculated as: {normalizedTempNew * eigenVal}")
#             break
#           temp = tempNew
#           if n == max_iter_pow - 1:
#             print(f"power method did not converge at n = {n}")
    return x, i + 1


def zero_grad(params):
    for p in params:
        if p.grad is not None:
            p.grad.detach()
            p.grad.zero_()
