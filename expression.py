from abc import ABC, abstractmethod
from numpy import format_float_positional
import numbers
import gurobipy as grb

# controls, if gurobi general constraints are used
# (right now only for binary multiplication)
# TODO: extend to ReLU, Max, ...
use_grb_native = True

default_bound = 999999
epsilon = 1e-8

def ffp(x):
    if x < 0:
        s = format_float_positional(-x, trim='-')
        return '(- ' + s + ')'
    else:
        return format_float_positional(x, trim='-')


def flatten(list):
    return [x for sublist in list for x in sublist]


def makeLeq(lhs, rhs):
    return '(assert (<= ' + lhs + ' ' + rhs + '))'


def makeGeq(lhs, rhs):
    # maybe switch to other representation later
    return makeLeq(rhs, lhs)


def makeEq(lhs, rhs):
    return '(assert (= ' + lhs + ' ' + rhs + '))'


def makeLt(lhs, rhs):
    return '(assert (< ' + lhs + ' ' + rhs + '))'


def makeGt(lhs, rhs):
    return makeLt(rhs, lhs)


class Expression(ABC):

    def __init__(self, net, layer, row):
        self.lo = -default_bound
        self.hi = default_bound
        self.hasLo = False
        self.hasHi = False

        self.net = net
        self.layer = layer
        self.row = row
        pass

    def getIndex(self):
        return (self.net, self.layer, self.row)

    @abstractmethod
    def to_smtlib(self):
        pass


    @abstractmethod
    def to_gurobi(self, model):
        pass

    def getHi(self):
        return self.hi

    def getLo(self):
        return self.lo

    def getLo_exclusive(self):
        return self.lo - epsilon

    def getHi_exclusive(self):
        return self.hi + epsilon

    @abstractmethod
    def tighten_interval(self):
        pass

    def update_bounds(self, l, h):
        if l > self.lo:
            self.lo = l
            self.hasLo = True
        if h < self.hi:
            self.hi = h
            self.hasHi = True


class Constant(Expression):

    def __init__(self, value, net, layer, row):
        # Any idea how to get rid of net, layer, row for constants?
        super(Constant, self).__init__(net, layer, row)
        self.value = value
        self.hi = value
        self.lo = value
        self.hasLo = True
        self.hasHi = True

    def tighten_interval(self):
        pass

    def to_smtlib(self):
        s = None
        if isinstance(self.value, numbers.Integral):
            if self.value < 0:
                s = '(- ' + str(-self.value) + ')'
            else:
                s = str(self.value)
        else:
            s = ffp(self.value)

        return s

    def to_gurobi(self, model):
        return self.value

    def __repr__(self):
        return str(self.value)


class Variable(Expression):

    def __init__(self, layer, row, netPrefix, prefix_name='x', type='Real'):
        super(Variable, self).__init__(netPrefix, layer, row)
        self.prefix_name = prefix_name

        self.name = ''
        if not netPrefix == '':
            self.name += netPrefix + '_'

        self.name += prefix_name + '_' + str(layer) + '_' + str(row)
        self.type = type

        self.hasLo = False
        self.hasHi = False
        self.lo = -default_bound
        self.hi = default_bound

        self.has_grb_var = False
        self.grb_var = None

    def tighten_interval(self):
        pass

    def to_smtlib(self):
        return self.name

    def get_smtlib_decl(self):
        return '(declare-const ' + self.name + ' ' + self.type + ')'

    def get_smtlib_bounds(self):
        bounds = ''
        if self.hasHi:
            bounds += makeLeq(self.name, ffp(self.hi))
        if self.hasLo:
            bounds += '\n' + makeGeq(self.name, ffp(self.lo))

        return bounds

    def register_to_gurobi(self, model):
        lower = - grb.GRB.INFINITY
        upper = grb.GRB.INFINITY
        var_type = None
        if self.hasHi:
            upper = self.hi
        if self.hasLo:
            lower = self.lo
        # only types used are Int and Real
        # for Int only 0-1 are used -> Binary for gurobi
        if self.type == 'Int':
            var_type = grb.GRB.BINARY
        else:
            var_type = grb.GRB.CONTINUOUS

        self.grb_var = model.addVar(lb=lower, ub=upper, vtype=var_type, name=self.name)
        self.has_grb_var = True

    def to_gurobi(self, model):
        return self.grb_var

    def setLo(self, val):
        self.hasLo = True
        self.lo = val

    def setHi(self, val):
        self.hasHi = True
        self.hi = val

    def __repr__(self):
        return self.name


class Sum(Expression):

    def __init__(self, terms):
        net, layer, row = terms[0].getIndex()
        super(Sum, self).__init__(net, layer, row)
        self.children = terms
        self.lo = -default_bound
        self.hi = default_bound

    def tighten_interval(self):
        l = 0
        h = 0
        for term in self.children:
            term.tighten_interval()
            l += term.getLo()
            h += term.getHi()

        super(Sum, self).update_bounds(l, h)

    def to_smtlib(self):
        sum = '(+'
        for term in self.children:
            sum += ' ' + term.to_smtlib()

        sum += ')'
        return sum

    def to_gurobi(self, model):
        return grb.quicksum([t.to_gurobi(model) for t in self.children])

    def __repr__(self):
        sum = '(' + str(self.children[0])
        for term in self.children[1:]:
            sum += ' + ' + str(term)
        sum += ')'

        return sum


class Neg(Expression):

    def __init__(self, input):
        net, layer, row = input.getIndex()
        super(Neg, self).__init__(net, layer, row)
        self.input = input
        self.hasHi = input.hasLo
        self.hasLo = input.hasHi
        self.lo = -input.getHi()
        self.hi = -input.getLo()

    def tighten_interval(self):
        l = -self.input.getHi()
        h = -self.input.getLo()
        super(Neg, self).update_bounds(l, h)

    def to_smtlib(self):
        return '(- ' + self.input.to_smtlib() + ')'

    def to_gurobi(self, model):
        return -self.input.to_gurobi(model)

    def __repr__(self):
        return '(- ' + str(self.input) + ')'


class Multiplication(Expression):

    def __init__(self, constant, variable):
        net, layer, row = variable.getIndex()
        super(Multiplication, self).__init__(net, layer, row)
        self.constant = constant
        self.variable = variable
        self.lo = -default_bound
        self.hi = default_bound

    def tighten_interval(self):
        val1 = self.constant.value * self.variable.getLo()
        val2 = self.constant.value * self.variable.getHi()
        l = min(val1, val2)
        h = max(val1, val2)

        super(Multiplication, self).update_bounds(l, h)

    def to_smtlib(self):
        return '(* ' + self.constant.to_smtlib() + ' ' + self.variable.to_smtlib() + ')'

    def to_gurobi(self, model):
        if not self.variable.has_grb_var:
            raise ValueError('Variable {v} has not been registered to gurobi model!'.format(v=self.variable.name))

        return self.constant.to_gurobi(model) * self.variable.to_gurobi(model)

    def __repr__(self):
        return '(' + str(self.constant) + ' * ' + str(self.variable) + ')'


class Linear(Expression):

    def __init__(self, input, output):
        net, layer, row = output.getIndex()
        super(Linear, self).__init__(net, layer, row)
        self.output = output
        self.input = input
        self.lo = input.getLo()
        self.hi = input.getHi()

    def tighten_interval(self):
        self.input.tighten_interval()
        l = self.input.getLo()
        h = self.input.getHi()
        super(Linear, self).update_bounds(l, h)
        self.output.update_bounds(l, h)

    def to_smtlib(self):
        return makeEq(self.output.to_smtlib(), self.input.to_smtlib())

    def to_gurobi(self, model):
        return model.addConstr(self.output.to_gurobi(model) == self.input.to_gurobi(model))

    def __repr__(self):
        return '(' + str(self.output) + ' = ' + str(self.input) + ')'


class Relu(Expression):

    def __init__(self, input, output, delta):
        net, layer, row = output.getIndex()
        super(Relu, self).__init__(net, layer, row)
        self.output = output
        self.input = input
        self.lo = 0
        self.hi = default_bound
        self.delta = delta
        self.delta.setLo(0)
        self.delta.setHi(1)

    def tighten_interval(self):
        self.input.tighten_interval()
        h = self.input.getHi()
        l = self.input.getLo()
        if h <= 0:
            # ReLU inactive delta=0
            self.hi = 0
            self.lo = 0
            self.output.update_bounds(0,0)
            self.delta.update_bounds(0,0)
        elif l > 0:
            # ReLU active delta=1
            super(Relu, self).update_bounds(l, h)
            self.output.update_bounds(l, h)
            self.delta.update_bounds(1,1)
        else:
            # don't know inactive/active
            super(Relu, self).update_bounds(l, h)
            self.output.update_bounds(l, h)

    def to_smtlib(self):
        # maybe better with asymmetric bounds
        m = max(abs(self.input.getLo()), abs(self.input.getHi()))

        dm = Multiplication(Constant(m, self.net, self.layer, self.row), self.delta)
        inOneMinusDM = Sum([self.input, Constant(m, self.net, self.layer, self.row), Neg(dm)])

        enc  = makeGeq(self.output.to_smtlib(), '0')
        enc += '\n' + makeGeq(self.output.to_smtlib(), self.input.to_smtlib())
        enc += '\n' + makeLeq(Sum([self.input, Neg(dm)]).to_smtlib(), '0')
        enc += '\n' + makeGeq(inOneMinusDM.to_smtlib(), '0')
        enc += '\n' + makeLeq(self.output.to_smtlib(), inOneMinusDM.to_smtlib())
        enc += '\n' + makeLeq(self.output.to_smtlib(), dm.to_smtlib())

        return enc

    def to_gurobi(self, model):
        c_name = 'ReLU_{layer}_{row}'.format(layer=self.layer, row=self.row)
        return model.addConstr(self.output.to_gurobi(model) == grb.max_(self.input.to_gurobi(model), 0), name=c_name)

    def __repr__(self):
        return str(self.output) + ' =  ReLU(' + str(self.input) + ')'


class Max(Expression):

    def __init__(self, in_a, in_b, output, delta):
        net, layer, row = output.getIndex()
        super(Max, self).__init__(net, layer, row)
        self.output = output
        self.in_a = in_a
        self.in_b = in_b
        self.lo = -default_bound
        self.hi = default_bound
        self.delta = delta
        self.delta.setLo(0)
        self.delta.setHi(1)

    def tighten_interval(self):
        la = self.in_a.getLo()
        ha = self.in_a.getHi()
        lb = self.in_b.getLo()
        hb = self.in_b.getHi()

        if la > hb:
            # a is maximum
            self.output.update_bounds(la, ha)
            self.delta.update_bounds(0,0)
            super(Max, self).update_bounds(la, ha)
        elif lb > ha:
            # b is maximum
            self.output.update_bounds(lb, hb)
            self.delta.update_bounds(1,1)
            super(Max, self).update_bounds(lb, hb)
        else:
            # don't know which entry is max
            l = max(la, lb)
            h = max(ha, hb)
            self.output.update_bounds(l, h)
            super(Max, self).update_bounds(l, h)

    def to_smtlib(self):
        # maybe better with asymmetric bounds
        la = self.in_a.getLo()
        ha = self.in_a.getHi()
        lb = self.in_b.getLo()
        hb = self.in_b.getHi()
        m = max(abs(la), abs(ha), abs(lb), abs(hb))

        dm = Multiplication(Constant(m, self.net, self.layer, self.row), self.delta)
        in_bOneMinusDM = Sum([self.in_b, Constant(m, self.net, self.layer, self.row), Neg(dm)])

        enc  = makeGeq(self.output.to_smtlib(), self.in_a.to_smtlib())
        enc += '\n' + makeGeq(self.output.to_smtlib(), self.in_b.to_smtlib())
        enc += '\n' + makeLeq(self.output.to_smtlib(), Sum([self.in_a, dm]).to_smtlib())
        enc += '\n' + makeLeq(self.output.to_smtlib(), in_bOneMinusDM.to_smtlib())

        return enc

    def to_gurobi(self, model):
        return model.addConstr(self.output.to_gurobi(model) == grb.max_(self.in_a.to_gurobi(model), self.in_b.to_gurobi(model)))

    def __repr__(self):
        return str(self.output) +  ' = max(' + str(self.in_a) + ', ' + str(self.in_b) + ')'


class One_hot(Expression):
    # returns 1, iff input >= 0, 0 otherwise

    def __init__(self, input, output):
        net, layer, row = output.getIndex()
        super(One_hot, self).__init__(net, layer, row)
        self.output = output
        self.input = input
        self.output.setLo(0)
        self.output.setHi(1)
        self.lo = 0
        self.hi = 1

    def tighten_interval(self):
        self.input.tighten_interval()
        l_i = self.input.getLo()
        h_i = self.input.getHi()

        if l_i >= 0:
            self.output.update_bounds(1, 1)
            super(One_hot, self).update_bounds(1, 1)
        elif h_i < 0:
            self.output.update_bounds(0, 0)
            super(One_hot, self).update_bounds(0, 0)

    def to_smtlib(self):
        l_i = self.input.getLo()
        h_i = self.input.getHi_exclusive()

        h_i_out = Multiplication(Constant(h_i, self.net, self.layer, self.row), self.output)
        l_i_const = Constant(l_i, self.net, self.layer, self.row)
        l_i_out = Multiplication(l_i_const, self.output)

        enc = makeGt(h_i_out.to_smtlib(), self.input.to_smtlib())
        enc += '\n' + makeGeq(self.input.to_smtlib(), Sum([l_i_const, Neg(l_i_out)]).to_smtlib())

        return enc

    def to_gurobi(self, model):
        l_i = self.input.getLo()
        h_i = self.input.getHi_exclusive()

        c_name = 'OneHot_{layer}_{row}'.format(layer=self.layer, row=self.row)

        # convert to greater than
        # normal (hi * output) - eps >= ... doesn't work
        c1 = model.addConstr(h_i * (self.output.to_gurobi(model) - epsilon) >= self.input.to_gurobi(model), name=c_name + '_a')
        c2 = model.addConstr(self.input.to_gurobi(model) >= (1 - self.output.to_gurobi(model)) * l_i, name=c_name + '_b')

        return c1, c2

    def __repr__(self):
        return str(self.output) + ' = OneHot(' + str(self.input) + ')'


class Greater_Zero(Expression):
    # returns 1, iff lhs > 0, 0 otherwise

    def __init__(self, lhs, delta):
        net, layer, row = delta.getIndex()
        super(Greater_Zero, self).__init__(net, layer, row)
        self.lhs = lhs
        self.delta = delta
        self.delta.setLo(0)
        self.delta.setHi(1)
        self.lo = 0
        self.hi = 1

    def tighten_interval(self):
        self.lhs.tighten_interval()
        l = self.lhs.getLo()
        h = self.lhs.getHi()

        if l > 0:
            self.delta.update_bounds(1, 1)
            super(Greater_Zero, self).update_bounds(1, 1)
        elif h <= 0:
            self.delta.update_bounds(0, 0)
            super(Greater_Zero, self).update_bounds(0, 0)

    def to_smtlib(self):
        l = self.lhs.getLo_exclusive()
        h = self.lhs.getHi()

        hd = Multiplication(Constant(h, self.net, self.layer, self.row), self.delta)
        l_const = Constant(l, self.net, self.layer, self.row)
        ld = Multiplication(l_const, self.delta)

        enc = makeLeq(self.lhs.to_smtlib(), hd.to_smtlib())
        enc += '\n' + makeGt(self.lhs.to_smtlib(), Sum([l_const, Neg(ld)]).to_smtlib())

        return enc

    def to_gurobi(self, model):
        l = self.lhs.getLo_exclusive()
        h = self.lhs.getHi()

        c_name = 'Gt0_{layer}_{row}'.format(layer=self.layer, row=self.row)

        c1 = model.addConstr(self.lhs.to_gurobi(model) <= h * self.delta.to_gurobi(model), name=c_name + '_a')
        # convert to greater than
        # with epsilon otherwise, when lhs == 0, delta == 1 would also be ok, with epsilon forced to take 0
        c2 = model.addConstr(self.lhs.to_gurobi(model) >= (1 - self.delta.to_gurobi(model) + epsilon) * l, name=c_name + '_b')

        return c1, c2

    def __repr__(self):
        return str(self.lhs) + ' > 0 <==> ' + str(self.delta) + ' = 1'


class Geq(Expression):
    # TODO: no return value as no real expression, just a constraint (better idea where to put it?)
    # could return 0/1 but would need more complicated delta stmt instead of just proxy for printing geq

    def __init__(self, lhs, rhs):
        net, layer, row = lhs.getIndex()
        super(Geq, self).__init__(net, layer, row)
        self.lhs = lhs
        self.rhs = rhs
        self.lo = 0
        self.hi = 1

    def tighten_interval(self):
        self.lhs.tighten_interval()
        self.rhs.tighten_interval()
        llhs = self.lhs.getLo()
        hlhs = self.lhs.getHi()
        lrhs = self.rhs.getLo()
        hrhs = self.rhs.getHi()

        if llhs >= hrhs:
            super(Geq, self).update_bounds(1, 1)
        elif hlhs < lrhs:
            super(Geq, self).update_bounds(0, 0)

    def to_smtlib(self):
        return makeGeq(self.lhs.to_smtlib(), self.rhs.to_smtlib())

    def to_gurobi(self, model):
        return model.addConstr(self.lhs.to_gurobi(model) >= self.rhs.to_gurobi(model))

    def __repr__(self):
        return str(self.lhs) + ' >= ' + str(self.rhs)


class BinMult(Expression):
    # multiplication of a binary variable and another expression
    # can be linearized and expressed by this expression

    def __init__(self, binvar, factor, result_var):
        net, layer, row = result_var.getIndex()
        super(BinMult, self).__init__(net, layer, row)
        self.binvar = binvar
        self.factor = factor
        self.result_var = result_var
        self.lo = -default_bound
        self.hi = default_bound

    def tighten_interval(self):
        self.factor.tighten_interval()
        fl = self.factor.getLo()
        fh = self.factor.getHi()

        # 0 <= bl <= bh <= 1
        bl = self.binvar.getLo()
        bh = self.binvar.getHi()

        l = min(bl * fl, bl * fh, bh * fl, bh * fh)
        h = max(bl * fl, bl * fh, bh * fl, bh * fh)

        self.result_var.update_bounds(l, h)
        super(BinMult, self).update_bounds(l, h)

    def to_smtlib(self):
        bigM = Constant(self.factor.getHi(), self.net, self.layer, self.row)
        bigMbinvar = Multiplication(bigM, self.binvar)

        enc = makeLeq(self.result_var.to_smtlib(), bigMbinvar.to_smtlib())
        enc += '\n' + makeLeq(self.result_var.to_smtlib(), self.factor.to_smtlib())
        enc += '\n' + makeLeq(Sum([self.factor, Neg(self.result_var)]).to_smtlib(), Sum([bigM, Neg(bigMbinvar)]).to_smtlib())

        return enc

    def to_gurobi(self, model):
        if not self.binvar.has_grb_var:
            raise ValueError('Variable {v} has not been registered to gurobi model!'.format(v=self.binvar.name))

        c_name = 'BinMult_{net}_{layer}_{row}'.format(net=self.net, layer=self.layer, row=self.row)

        ret_constr = None

        if use_grb_native:
            model.addConstr((self.binvar.to_gurobi() == 0) >> (self.result_var.to_gurobi() == 0), name=c_name + '_1')
            ret_constr = model.addConstr((self.binvar.to_gurobi() == 1)
                                         >> (self.result_var.to_gurobi() == self.factor.to_gurobi()), name=c_name + '_2')
        else:
            bigM = self.factor.getHi()

            model.addConstr(self.result_var.to_gurobi(model) <= bigM * self.binvar.to_gurobi(model), name=c_name + '_1')
            model.addConstr(self.factor.to_gurobi(model) - self.result_var.to_gurobi(model)
                            <= (1 - self.binvar.to_gurobi(model)) * bigM, name=c_name + '_2')
            ret_constr = model.addConstr(self.result_var.to_gurobi(model) <= self.factor.to_gurobi(model), name=c_name + '_3')

        # return last added constraint, don't know what to return instead and all other to_gurobis return a constraint
        return ret_constr

    def __repr__(self):
        return str(self.result_var) + ' = BinMult(' + str(self.binvar) + ', ' + str(self.factor) + ')'


